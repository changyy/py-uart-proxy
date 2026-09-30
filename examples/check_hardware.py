"""
Acceptance check against a REAL serial adapter — what CI cannot cover.

The test suite runs on ptys, and the pty driver ignores ``TIOCEXCL``, so some
guarantees are only observable with hardware. Plug an adapter in and run:

    python examples/check_hardware.py /dev/tty.usbserial-110
    python examples/check_hardware.py                 # choose from a list
    python examples/check_hardware.py DEV --replug    # + unplug/replug steps
    python examples/check_hardware.py DEV --loopback  # + data, TX jumpered to RX

Use the Python that has this checkout of uart-proxy installed (the dev venv, or
``~/.local/pipx/venvs/uart-proxy/bin/python`` after ``pipx install --force .``).

What it checks, in order:

1. **Identity** (nothing opened) — the adapter is enumerated, listed ahead of
   ports without USB identity, never described as "n/a"; a throwaway
   ``--profile`` with its VID/PID finds it and applies its settings (S27, S30,
   S31).
2. **Exclusive claim** (S15) — with the port open, a second open of the same
   node and of its macOS twin (cu/tty) is refused with EBUSY; closing releases
   it. Also reports what ``--no-exclusive`` measures on this machine.
3. **Held by a session** (S21, S23) — a real ``connect --serve`` holds it; an
   open from here is flagged busy and the hint names that session;
   ``start`` refuses the port; ``status --show-auth --json`` lists it with its
   code; a client joins with the registry's details; exiting unregisters it.
4. ``--loopback`` — bytes written come back (TX wired to RX).
5. ``--replug`` — you unplug and replug; the session notices, reconnects, and —
   if the adapter came back under another name — follows it (S31).

Safety: it stops before opening anything if another process holds the port
(``lsof``), and asks before opening — opening a serial port toggles DTR/RTS,
which resets some boards. Nothing is written to the device unless
``--loopback``. Sessions and state live in a temporary ``UART_PROXY_HOME``, so
your real ones are never touched.

Exits non-zero and prints ``FAIL: …`` per failed check.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time


class Checks:
    """Collects pass/fail so one bad check doesn't hide the rest."""

    def __init__(self, verbose: bool) -> None:
        self.failures: list[str] = []
        self.verbose = verbose

    def ok(self, name: str, condition: bool, detail: str = "") -> bool:
        mark = "✓" if condition else "✗"
        shown = f"   {detail}" if detail and (self.verbose or not condition) else ""
        print(f"  {mark} {name}{shown}")
        if not condition:
            self.failures.append(name + (f" — {detail}" if detail else ""))
        return condition

    def info(self, text: str) -> None:
        print(f"  · {text}")

    @staticmethod
    def section(title: str) -> None:
        print(f"\n{title}")


def _wait(predicate, timeout: float, step: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return bool(predicate())


def _cli(*argv: str, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "uart_proxy", *argv],
                          capture_output=True, text=True, timeout=timeout)


def _open_error(path: str):
    """Try a plain pyserial open; return None on success, else the exception."""
    import serial

    try:
        port = serial.Serial(path)
    except (serial.SerialException, OSError) as exc:
        return exc
    port.close()
    return None


def _is_ebusy(exc) -> bool:
    from uart_proxy.core.port_busy import is_busy_error

    return exc is not None and is_busy_error(exc)


# ── choosing the device ─────────────────────────────────────────────────────


def choose_device(given: str | None) -> str | None:
    from uart_proxy.cli import _scan_ports
    from uart_proxy.core.port_identity import describe, sort_ports

    if given:
        return given
    ports = sort_ports(_scan_ports())
    if not ports:
        print("No serial ports found. Plug the adapter in and try again.")
        return None
    if not sys.stdin.isatty():
        print("Give the device path as an argument (no terminal to choose in).")
        return None
    print("Serial ports on this machine:")
    for i, ident in enumerate(ports, 1):
        print(f"  {i}. {ident.tty_device}  {describe(ident)}".rstrip())
    try:
        answer = input("Device path, or its number: ").strip()
    except EOFError:
        return None
    if answer.isdigit() and 1 <= int(answer) <= len(ports):
        return ports[int(answer) - 1].tty_device
    return answer or None


def preflight(device: str, assume_yes: bool) -> bool:
    from uart_helper import PortIdentity

    from uart_proxy.core.port_busy import find_holders

    device = PortIdentity(device=device).tty_device
    if not os.path.exists(device):
        print(f"{device} does not exist.")
        return False
    holders = find_holders(device)
    if holders:
        who = ", ".join(f"{h.command} (pid {h.pid})" for h in holders)
        print(f"{device} is open in {who}.\n"
              f"Close it first — this check needs the port to itself.")
        return False
    if assume_yes:
        return True
    print(f"This opens {device} several times. Opening a serial port toggles "
          f"DTR/RTS,\nwhich resets some boards. Nothing is written to it unless "
          f"--loopback.")
    try:
        answer = input("Continue? [y/N] ")
    except EOFError:  # no terminal to answer from: that is a no
        print("\n(no answer — pass --yes to run without asking)")
        return False
    return answer.strip().lower() in ("y", "yes")


# ── 1. identity ─────────────────────────────────────────────────────────────


def check_identity(c: Checks, device: str):
    from uart_proxy import cli
    from uart_proxy.core.port_busy import same_device
    from uart_proxy.core.port_identity import describe, has_usb_identity, sort_ports

    c.section("1. Identity (nothing is opened)")
    ports = cli._scan_ports()
    ident = next((p for p in ports if same_device(p.tty_device, device)), None)
    if not c.ok("the adapter is enumerated", ident is not None, device):
        return None
    c.info(f"{ident.tty_device}  {describe(ident)}".rstrip())
    usb = has_usb_identity(ident)
    if not usb:
        c.info("no USB identity (no VID/PID): --profile matching and following a "
               "replug do not apply to this port")
        return ident
    ordered = sort_ports(ports)
    first_non_usb = next((i for i, p in enumerate(ordered) if not has_usb_identity(p)),
                         len(ordered))
    position = next(i for i, p in enumerate(ordered) if p is ident)
    c.ok("listed ahead of ports without USB identity", position < first_non_usb)
    c.ok('never described as "n/a"', "n/a" not in describe(ident).lower())
    for other in ports:
        if "n/a" in describe(other).lower():
            c.ok(f'{other.tty_device} is not described as "n/a"', False)

    # A throwaway profile made from this adapter's own VID/PID.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "hwcheck.toml")
        with open(path, "w") as fh:
            fh.write(f'[defaults]\nbaudrate = 57600\n\n[[rules]]\n'
                     f'vid = "{ident.vid:04x}"\npid = "{ident.pid:04x}"\n')
            if ident.serial_number:
                fh.write(f'serial = "{ident.serial_number}"\n')
        args = cli.build_parser().parse_args(["connect", "--profile", path])
        error = cli.apply_profile(args)
        c.ok("a --profile with its VID/PID loads", error is None, error or "")
        if error is None:
            found = cli.resolve_port(args, allow_picker=False)
            c.ok("…and finds this port by its rules",
                 found is not None and same_device(found, device), str(found))
            c.ok("…and applies its [defaults]", args.baud == 57600, f"baud={args.baud}")
    return ident


# ── 2. exclusive claim ──────────────────────────────────────────────────────


def check_exclusive(c: Checks, device: str) -> None:
    from uart_proxy.core.port_busy import paired_nodes
    from uart_proxy.io.uart_source import UartSource

    c.section("2. Exclusive claim (S15)")
    source = UartSource(device)
    try:
        source.open()
    except Exception as exc:  # noqa: BLE001
        c.ok("the port opens", False, str(exc))
        return
    try:
        c.ok("the claim is taken (TIOCEXCL)", source.is_exclusive)
        same = _open_error(source.device_path)
        c.ok("a second open of the same node is refused with EBUSY", _is_ebusy(same),
             repr(same) if same else "it opened")
        for twin in paired_nodes(source.device_path)[1:]:
            if os.path.exists(twin):
                err = _open_error(twin)
                c.ok(f"…and of its twin {os.path.basename(twin)}", _is_ebusy(err),
                     repr(err) if err else "it opened")
    finally:
        source.close()
    c.ok("closing releases the claim", _open_error(source.device_path) is None)

    # What --no-exclusive leaves open, measured here (SPEC S15's table).
    loose = UartSource(device, exclusive=False)
    try:
        loose.open()
        err = _open_error(loose.device_path)
        c.info("with --no-exclusive a second open of the same node "
               + ("SUCCEEDS — two readers would split the stream" if err is None
                  else f"is refused ({err})"))
    except Exception as exc:  # noqa: BLE001
        c.info(f"--no-exclusive open failed: {exc}")
    finally:
        loose.close()


# ── 3. held by a session ────────────────────────────────────────────────────


class _Lines:
    """Collect a subprocess's stderr lines on a thread."""

    def __init__(self, stream) -> None:
        self.lines: list[str] = []
        self._thread = threading.Thread(target=self._pump, args=(stream,), daemon=True)
        self._thread.start()

    def _pump(self, stream) -> None:
        for line in stream:
            self.lines.append(line.rstrip("\n"))

    def text(self) -> str:
        return "\n".join(self.lines)


def check_held(c: Checks, device: str) -> None:
    from uart_proxy.core.daemon import connect_host, list_daemons
    from uart_proxy.core.port_busy import describe_busy
    from uart_proxy.io.socket_source import SocketSource
    from uart_proxy.io.uart_source import UartSource

    c.section("3. Held by a session (S21, S23)")
    code = f"hwcheck-{secrets.token_hex(4)}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port", device,
         "--no-tui", "--no-log", "--serve", "--listen", "127.0.0.1",
         "--listen-port", "0", "--auth", code],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        text=True)
    err = _Lines(proc.stderr)
    try:
        registered = _wait(lambda: "Registered as" in err.text(), 20)
        if not c.ok("a `connect --serve` holds the port and registers",
                    registered, err.text()[-300:]):
            return
        _wait(lambda: "claimed" in err.text() or "exclusive" in err.text(), 5)
        name = err.text().split("Registered as '", 1)[1].split("'", 1)[0]

        probe = UartSource(device)
        try:
            probe.open()
            probe.close()
            c.ok("an open from here is refused", False, "it opened")
        except Exception:  # noqa: BLE001
            c.ok("an open from here is refused and flagged busy", probe.busy)
        hint = describe_busy(device)
        c.ok("the busy hint names the session and `attach`",
             f"'{name}'" in hint and f"uart-proxy attach {name}" in hint, hint)

        started = _cli("start", "--port", device, "--name", "hwcheck-second")
        c.ok("`start` refuses the port", started.returncode != 0
             and "already held by" in started.stderr, started.stderr.strip()[-200:])

        listed = _cli("status", "--json", "--show-auth")
        entries = json.loads(listed.stdout).get("data", []) if listed.stdout else []
        entry = next((e for e in entries if e["name"] == name), None)
        c.ok("`status --json --show-auth` lists it, foreground, with its code",
             entry is not None and entry.get("foreground") is True
             and entry.get("auth") == {code: "full"}, listed.stdout.strip()[-300:])

        info = next((d for d in list_daemons() if d.name == name), None)
        if info is not None:
            client = SocketSource(connect_host(info.listen_host), info.listen_port,
                                  info.auth)
            try:
                client.open()
                c.ok("a client joins with the registry's details", True)
            except Exception as exc:  # noqa: BLE001
                c.ok("a client joins with the registry's details", False, str(exc))
            finally:
                client.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    c.ok("exiting unregisters it", not list_daemons(include_dead=True))
    c.ok("…and releases the port", _wait(lambda: _open_error(device) is None, 5))


# ── 4. loopback ─────────────────────────────────────────────────────────────


def check_loopback(c: Checks, device: str) -> None:
    from uart_proxy.io.uart_source import UartSource

    c.section("4. Loopback — TX wired to RX")
    source = UartSource(device)
    source.open()
    try:
        payload = f"uart-proxy-loopback-{secrets.token_hex(4)}\r\n".encode()
        source.write(payload)
        got = b""
        deadline = time.monotonic() + 3
        while payload not in got and time.monotonic() < deadline:
            got += source.read(4096, 0.2)
        c.ok("what is written comes back", payload in got,
             f"sent {payload!r}, got {got[:80]!r}")
    finally:
        source.close()


# ── 5. replug ───────────────────────────────────────────────────────────────


def check_replug(c: Checks, device: str) -> None:
    from uart_proxy.cli import attach_move_report
    from uart_proxy.core.events import EventKind
    from uart_proxy.core.session import UartSession
    from uart_proxy.io.uart_source import UartSource

    c.section("5. Replug (S12, S31)")
    source = UartSource(device)
    session = UartSession(source, reconnect_interval=0.5)
    events: list = []
    session.bus.subscribe(events.append)
    attach_move_report(session, source)
    session.start()
    try:
        if not c.ok("connected", _wait(lambda: session.is_connected, 10)):
            return
        before = source.device_path
        input("\n  Unplug the adapter, then press Enter… ")
        c.ok("the unplug is noticed", _wait(lambda: not session.is_connected, 15))
        input("  Plug it back in — into a DIFFERENT USB socket if you can, so it "
              "may get a new name — then press Enter… ")
        c.ok("it reconnects on its own", _wait(lambda: session.is_connected, 60))
        after = source.device_path
        if after == before:
            c.info(f"it came back on the same path ({after}); the rename case "
                   f"(S31) was not exercised — try another socket")
        else:
            notices = [e.text for e in events if e.kind is EventKind.NOTICE]
            c.ok(f"it was followed to its new name: {before} → {after}",
                 any("device moved" in n for n in notices), "\n".join(notices))
    finally:
        session.stop()


# ── main ────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("device", nargs="?", help="The adapter, e.g. /dev/tty.usbserial-110")
    ap.add_argument("--yes", action="store_true", help="Don't ask before opening it.")
    ap.add_argument("--replug", action="store_true",
                    help="Also run the unplug/replug steps (asks you to act).")
    ap.add_argument("--loopback", action="store_true",
                    help="Also write and read back (TX must be wired to RX).")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if os.name != "posix":
        print("This check uses POSIX tools (TIOCEXCL, lsof); run it on macOS or Linux.")
        return 2
    device = choose_device(args.device)
    if not device:
        return 2
    from uart_helper import PortIdentity

    device = PortIdentity(device=device).tty_device
    if not preflight(device, args.yes):
        return 2

    # Everything below keeps its sessions away from the real ~/.uart-proxy.
    home = tempfile.mkdtemp(prefix="uart-proxy-hwcheck-")
    os.environ["UART_PROXY_HOME"] = home
    print(f"\nChecking {device}  (state in {home})")

    c = Checks(args.verbose)
    ident = check_identity(c, device)
    if ident is not None:
        check_exclusive(c, device)
        check_held(c, device)
        if args.loopback:
            check_loopback(c, device)
        if args.replug:
            check_replug(c, device)

    print()
    if c.failures:
        for failure in c.failures:
            print(f"FAIL: {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        raise SystemExit(130)
