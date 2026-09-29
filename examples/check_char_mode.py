"""
Acceptance check for character mode and its terminal view (SPEC S19, S20) —
no hardware needed.

`tests/test_terminal.py` proves the emulation and `tests/test_tui_terminal.py`
proves the wiring, both against a fake device that does exactly what the test
asked for. This script checks the claim those cannot: that a **real interactive
shell** is usable through the TUI. A pty pair stands in for the serial adapter
and a real ``bash`` sits on the far end, so every behaviour here is the shell's,
not a fixture's.

    bash (its own session, on the pty slave)
             ↕
    pty master  ==  the DataSource the TUI reads and writes
             ↕
    UartProxyApp in character mode, driven by Textual's pilot

Checks: Tab really completes a filename, and does it **in place** rather than
one row per keystroke — the bug this feature exists to fix · ``^C`` abandons the
line and returns a prompt · ANSI colour survives into the rendered screen · ↑
recalls the previous command from the shell's own history.

Run it:

    python examples/check_char_mode.py         # exit 0 on success
    python examples/check_char_mode.py -v      # show the screen after each step

Exits non-zero and prints ``FAIL: …`` per failed check, so it can be a CI step.
POSIX only — it needs ``pty`` — which is also true of the mirrors check beside it.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios

from uart_proxy.core.session import UartSession
from uart_proxy.io.source import DataSource
from uart_proxy.ui.terminal import PYTE_AVAILABLE
from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE, UartProxyApp

#: The shell is told 80x24 explicitly. A serial line cannot carry a window size
#: (SPEC S20), so over a real wire the device keeps whatever it assumed; here we
#: set it on the pty so readline wraps where the emulated screen does.
COLUMNS, LINES = 80, 24


class Checks:
    """Collects pass/fail so one bad check doesn't hide the rest."""

    def __init__(self, verbose: bool) -> None:
        self.failures: list[str] = []
        self.verbose = verbose

    def ok(self, name: str, condition: bool, detail: str = "") -> None:
        mark = "✓" if condition else "✗"
        print(f"  {mark} {name}" + (f"   {detail}" if detail else ""))
        if not condition:
            self.failures.append(f"{name}" + (f" — {detail}" if detail else ""))

    def note(self, text: str) -> None:
        if self.verbose:
            print(f"    {text}")


class PtySource(DataSource):
    """The pty master, dressed as a serial port."""

    def __init__(self, fd: int) -> None:
        self.fd = fd

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self, max_bytes: int, timeout: float) -> bytes:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return b""
        try:
            return os.read(self.fd, max_bytes)
        except OSError:            # the shell exited and closed its end
            return b""

    def write(self, data: bytes) -> int:
        return os.write(self.fd, data)

    def description(self) -> str:
        return "pty → bash"


def start_shell(workdir: str) -> tuple[int, subprocess.Popen]:
    """An interactive bash on its own pty, with a predictable prompt."""
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ,
                struct.pack("HHHH", LINES, COLUMNS, 0, 0))
    proc = subprocess.Popen(
        ["/bin/bash", "--norc", "--noprofile", "-i"],
        stdin=slave, stdout=slave, stderr=slave, cwd=workdir,
        # Its own session, so it gets the pty as a controlling terminal and
        # behaves interactively: echo, readline, job control, completion.
        start_new_session=True,
        env={**os.environ, "PS1": "$ ", "TERM": "xterm"},
    )
    os.close(slave)
    return master, proc


async def run_checks(checks: Checks) -> None:
    workdir = tempfile.mkdtemp(prefix="uart-proxy-charmode-")
    open(os.path.join(workdir, "README.md"), "w").close()
    master, proc = start_shell(workdir)
    session = UartSession(PtySource(master), auto_reconnect=False, default_eol=b"\r")
    app = UartProxyApp(session, input_mode="char")

    def rows() -> list[str]:
        return [r.rstrip() for r in app._term.emulator.display if r.strip()]

    try:
        async with app.run_test(size=(COLUMNS, LINES)) as pilot:

            async def settle(ticks: int = 12) -> None:
                for _ in range(ticks):
                    await asyncio.sleep(0.05)
                    await pilot.pause()

            async def type_out(text: str) -> None:
                """One key at a time, with a gap longer than the session's
                0.2s idle flush — the timing that used to put every echoed
                character on a row of its own."""
                for char in text:
                    await pilot.press("space" if char == " " else char)
                    await asyncio.sleep(0.22)
                    await pilot.pause()

            await settle(20)
            checks.note(f"shell prompt: {rows()[-1]!r}" if rows() else "no prompt yet")

            # 1 & 2 — completion, and that typing did not fragment into rows
            await type_out("cat REA")
            await settle(8)
            checks.note(f"before TAB: {rows()[-1]!r}")
            typed_rows = [r for r in rows() if "cat" in r]
            checks.ok("a typed command occupies one row, not one per character",
                      len(typed_rows) == 1, f"{len(typed_rows)} row(s)")

            await pilot.press("tab")
            await settle(20)
            checks.note(f"after TAB:  {rows()[-1]!r}")
            checks.ok("Tab completed the filename, in place",
                      any("cat README.md" in row for row in rows()),
                      rows()[-1] if rows() else "")

            # 3 — ^C abandons the line
            await pilot.press("ctrl+c")
            await settle(15)
            checks.note(f"after ^C:   {rows()[-1]!r}")
            checks.ok("^C abandoned the line and returned a prompt",
                      "README.md" not in rows()[-1],
                      rows()[-1])

            # 4 — colour reaches the screen
            await type_out("printf '\\033[31mRED\\033[0m\\n'")
            await pilot.press("enter")
            await settle(20)
            text = app._term.emulator.render()
            red = any(span.style is not None and span.style.color is not None
                      and span.style.color.name == "red" for span in text.spans)
            checks.ok("ANSI colour survived into the rendered screen", red)

            # 5 — the shell's own history, via the arrow key
            await pilot.press("up")
            await settle(15)
            checks.note(f"after UP:   {rows()[-1]!r}")
            checks.ok("↑ recalled the previous command from shell history",
                      "printf" in rows()[-1], rows()[-1])
    finally:
        session.stop()
        proc.kill()
        os.close(master)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show the screen after each step.")
    args = parser.parse_args()

    if not (_TEXTUAL_AVAILABLE and PYTE_AVAILABLE):
        print("textual and pyte are both required:  pip install textual pyte")
        return 2
    if not hasattr(os, "fork") or not os.path.exists("/bin/bash"):
        print("POSIX with /bin/bash required.")
        return 2

    print(f"character mode against a real bash on a pty ({COLUMNS}x{LINES})\n")
    checks = Checks(args.verbose)
    asyncio.run(run_checks(checks))

    print()
    for failure in checks.failures:
        print(f"FAIL: {failure}")
    print("all checks passed" if not checks.failures
          else f"{len(checks.failures)} check(s) failed")
    return 1 if checks.failures else 0


if __name__ == "__main__":
    sys.exit(main())
