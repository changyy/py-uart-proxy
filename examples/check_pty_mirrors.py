"""
Acceptance check for the PTY mirrors (SPEC S14) — no hardware needed.

`tests/test_pty_proxy.py` drives ``PtyProxyGroup`` directly. This script checks
the layer above: it launches the **real CLI** as a subprocess and behaves like
the two parties it is built for — a device on one end, two tools sharing it on
the other. A PTY pair stands in for the serial adapter, so it runs anywhere
POSIX.

    device master (this script)  <->  device slave  ==  --port given to uart-proxy
                                                             │
                                              two mirrors in --proxy-dir
                                                  │                │
                                              "agent"          "human"

Checks, in both merge modes: RX is broadcast to every mirror · a mirror's TX
reaches the device · the device's reply is visible to the mirror that did *not*
type · SIGTERM still removes the symlinks. Then per mode — `raw`: a single byte
and a `^C` cross without waiting for Enter; `line`: concurrent writers never
splice one command into another, yet a `^C` still overtakes the held buffer.

Run it:

    python examples/check_pty_mirrors.py            # both modes, exit 0 on success
    python examples/check_pty_mirrors.py --tx-merge raw
    python examples/check_pty_mirrors.py -v         # show every byte exchanged

Exits non-zero and prints ``FAIL: …`` per failed check, so it can be a CI step.
What it cannot cover is the exclusive claim (SPEC S15): the pty driver ignores
``TIOCEXCL``, so that one needs a real adapter — see SPEC S15's manual check.
"""

from __future__ import annotations

import argparse
import os
import pty
import selectors
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tty

_SETTLE = 0.2   # how long to let bytes cross the pipes before reading
_MIRRORS = 2


class Checks:
    """Collects pass/fail so one bad check doesn't hide the rest."""

    def __init__(self, verbose: bool) -> None:
        self.failures: list[str] = []
        self.verbose = verbose

    def ok(self, name: str, condition: bool, detail: str = "") -> None:
        mark = "✓" if condition else "✗"
        print(f"  {mark} {name}" + (f"   {detail}" if detail and self.verbose else ""))
        if not condition:
            self.failures.append(f"{name}" + (f" — {detail}" if detail else ""))

    def note(self, text: str) -> None:
        if self.verbose:
            print(f"    {text}")


def read_for(fd: int, seconds: float = 1.0) -> bytes:
    """Drain ``fd`` for up to ``seconds`` (it may arrive in several chunks)."""
    got = bytearray()
    end = time.monotonic() + seconds
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while time.monotonic() < end:
            if not sel.select(max(0.0, end - time.monotonic())):
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            got += chunk
    return bytes(got)


def _tail_stderr(stream, sink: list[str]) -> None:
    for raw in iter(stream.readline, b""):
        sink.append(raw.decode(errors="replace").rstrip("\n"))


def wait_for_mirrors(lines: list[str], proc, count: int, timeout: float = 15.0):
    """Read the mirror paths out of what uart-proxy announced on stderr.

    Parsing its own output (rather than guessing the filenames) is what makes
    this an end-to-end check: if the naming rule or the banner regressed, this
    is where it shows up.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [line.split("->")[0].strip() for line in lines if "->" in line]
        if len(found) >= count:
            return found[:count]
        if proc.poll() is not None:
            raise RuntimeError(
                "uart-proxy exited early:\n" + "\n".join(lines)
            )
        time.sleep(0.1)
    raise RuntimeError("mirrors never appeared:\n" + "\n".join(lines))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--python", default=sys.executable,
                    help="Interpreter that has uart-proxy installed "
                         "(default: the one running this script).")
    ap.add_argument("--proxy-dir", default=None,
                    help="Where the mirror symlinks go (default: a temp dir, "
                         "removed afterwards).")
    ap.add_argument("--tx-merge", choices=("raw", "line", "both"), default="both",
                    help="Which merge mode to check (default: both).")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print the bytes exchanged at each step.")
    args = ap.parse_args()

    modes = ("raw", "line") if args.tx_merge == "both" else (args.tx_merge,)
    failures: list[str] = []
    for merge in modes:
        print(f"\n{'=' * 60}\n--tx-merge {merge}\n{'=' * 60}")
        failures += [f"[{merge}] {f}" for f in check_one(args, merge)]

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("all checks passed")
    return 0


def check_one(args, merge: str) -> list[str]:
    checks = Checks(args.verbose)
    proxy_dir = args.proxy_dir or tempfile.mkdtemp(prefix="uart-proxy-check-")
    owns_dir = args.proxy_dir is None
    os.makedirs(proxy_dir, exist_ok=True)

    dev_master, dev_slave = pty.openpty()
    tty.setraw(dev_master)
    tty.setraw(dev_slave)
    device = os.ttyname(dev_slave)
    print(f"fake device: {device}\nproxy dir:   {proxy_dir}\n")

    proc = subprocess.Popen(
        [args.python, "-m", "uart_proxy", "connect", "--port", device,
         "--no-tui", "--no-log", "--tx-merge", merge,
         "--proxy-dir", proxy_dir, "--proxy-count", str(_MIRRORS)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    os.close(dev_slave)  # uart-proxy owns the slave now; we drive the master

    stderr_lines: list[str] = []
    threading.Thread(
        target=_tail_stderr, args=(proc.stderr, stderr_lines), daemon=True
    ).start()

    agent = human = None
    try:
        links = wait_for_mirrors(stderr_lines, proc, _MIRRORS)
        for link in links:
            checks.note(f"{link} -> {os.readlink(link)}")
        checks.ok(f"{_MIRRORS} mirrors announced and symlinked",
                  all(os.path.islink(link) for link in links),
                  ", ".join(os.path.basename(link) for link in links))
        checks.ok("mirrors are numbered from 0",
                  [os.path.basename(link).rsplit("-", 1)[-1] for link in links]
                  == [str(i) for i in range(_MIRRORS)])

        agent = os.open(links[0], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        human = os.open(links[1], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        tty.setraw(agent)
        tty.setraw(human)

        # 1. the device speaks — everyone hears it
        os.write(dev_master, b"U-Boot 2026.01\r\n=> ")
        seen_a, seen_h = read_for(agent), read_for(human)
        checks.note(f"agent={seen_a!r}  human={seen_h!r}")
        checks.ok("device RX is broadcast to every mirror",
                  b"U-Boot" in seen_a and b"U-Boot" in seen_h)

        # 2. a mirror types — the device receives it
        os.write(human, b"version\r")
        on_wire = read_for(dev_master)
        checks.note(f"wire={on_wire!r}")
        checks.ok("a mirror's TX reaches the device", b"version" in on_wire)

        # 3. mode-specific: is a byte immediate (raw) or held to keep the
        #    command whole (line)?
        if merge == "raw":
            os.write(human, b"a")
            on_wire = read_for(dev_master, 0.5)
            checks.note(f"wire={on_wire!r}")
            checks.ok("a single byte crosses without waiting for Enter",
                      on_wire == b"a", repr(on_wire))
            os.write(human, b"\x03")
            on_wire = read_for(dev_master, 0.5)
            checks.ok("^C reaches the device immediately",
                      on_wire == b"\x03", repr(on_wire))
        else:
            os.write(agent, b"reb")
            os.write(human, b"who")
            time.sleep(_SETTLE)
            mid = read_for(dev_master, 0.3)
            checks.ok("a partial line is held back", mid == b"", f"leaked {mid!r}")
            os.write(agent, b"oot\r")
            os.write(human, b"ami\r")
            on_wire = read_for(dev_master)
            checks.note(f"wire={on_wire!r}")
            checks.ok("both commands arrive intact (line-atomic merge)",
                      b"reboot\r" in on_wire and b"whoami\r" in on_wire, repr(on_wire))
            # …but a signal must still overtake the buffer it is queued behind.
            os.write(human, b"ls")
            time.sleep(_SETTLE)
            os.write(human, b"\x03")
            on_wire = read_for(dev_master, 0.5)
            checks.ok("^C is not held by line-merge",
                      on_wire == b"ls\x03", repr(on_wire))

        # 4. the reply is visible to the mirror that did NOT type
        os.write(dev_master, b"\r\nWed Jul 30 2026\r\n")
        seen_a, seen_h = read_for(agent), read_for(human)
        checks.note(f"agent={seen_a!r}  human={seen_h!r}")
        checks.ok("the device's reply is visible to both mirrors",
                  b"Wed Jul" in seen_a and b"Wed Jul" in seen_h)
    except RuntimeError as exc:
        print(f"  ✗ startup: {exc}")
        checks.failures.append(f"startup: {exc}")
    finally:
        for fd in (agent, human):
            if fd is not None:
                os.close(fd)

        # 5. SIGTERM (not just Ctrl-C) must still clean the symlinks up
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            checks.failures.append("did not exit on SIGTERM")
        leftover = [name for name in os.listdir(proxy_dir)]
        checks.ok("SIGTERM removes the symlinks", not leftover, f"left {leftover}")

        os.close(dev_master)
        if owns_dir:
            shutil.rmtree(proxy_dir, ignore_errors=True)

    if args.verbose:
        print("\n--- uart-proxy stderr ---")
        print("\n".join(stderr_lines))

    return checks.failures


if __name__ == "__main__":
    raise SystemExit(main())
