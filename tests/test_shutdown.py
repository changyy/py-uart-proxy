"""S16: ordered shutdown on SIGINT / SIGTERM, and what `kill -9` leaves behind."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from uart_proxy.cli import _trap_sigterm, close_recorder
from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.recorder import Recorder
from uart_proxy.core.timestamp import TimestampTracker

POSIX = os.name == "posix"


# ── the SIGTERM trap ────────────────────────────────────────────────────────


@pytest.mark.skipif(not POSIX, reason="SIGTERM is POSIX-only")
def test_the_trap_is_installed_and_restores_the_previous_handler():
    original = signal.getsignal(signal.SIGTERM)
    restore = _trap_sigterm()
    assert restore is not None
    assert signal.getsignal(signal.SIGTERM) is not original
    restore()
    assert signal.getsignal(signal.SIGTERM) is original


@pytest.mark.skipif(not POSIX, reason="SIGTERM is POSIX-only")
def test_the_trap_turns_sigterm_into_keyboardinterrupt():
    """That is the whole mechanism: KeyboardInterrupt unwinds the main thread,
    so the `finally` that stops the mirrors and the proxy actually runs."""
    restore = _trap_sigterm()
    try:
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGTERM, None)
    finally:
        restore()


# ── the log summary ─────────────────────────────────────────────────────────


def _rx(text: str) -> Event:
    return Event(EventKind.DATA, Direction.RX, TimestampTracker().stamp(),
                 data=text.encode(), text=text)


def test_the_written_files_are_reported(tmp_path):
    recorder = Recorder(str(tmp_path), base_name="output")
    recorder.handle(_rx("hello\n"))
    written = close_recorder(recorder)
    assert len(written) == 3
    assert all(os.path.exists(path) for path in written)


def test_paths_must_be_read_before_close(tmp_path):
    """Pins the trap that made the summary dead code: `paths` comes from the
    open handles, so after close() there is nothing left to report."""
    recorder = Recorder(str(tmp_path), base_name="output")
    recorder.handle(_rx("hello\n"))
    assert recorder.paths, "paths should be known while the files are open"
    recorder.close()
    assert recorder.paths == []


# ── end to end: does `kill` really unwind, in every mode? ───────────────────


def _connect(tmp_path, *extra):
    """Start `connect` against a PTY standing in for the device."""
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect",
         "--port", os.ttyname(slave), "--no-tui",
         "--output-dir", str(tmp_path / "logs"), *extra],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    os.close(slave)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:  # wait until it is actually connected
        if proc.poll() is not None:
            raise RuntimeError(proc.stderr.read().decode())
        if (tmp_path / "logs" / "output.log").exists():
            break
        time.sleep(0.1)
    return proc, master


@pytest.mark.skipif(not POSIX, reason="needs pty + POSIX signals")
@pytest.mark.parametrize("extra", [(), ("--serve", "--auth", "123456")],
                         ids=["plain", "serve"])
def test_sigterm_unwinds_in_every_mode(tmp_path, extra):
    """A negative return code means the signal killed the process outright and
    no `finally` ran. This used to be the case unless --proxy-dir was given,
    which made an ordered shutdown depend on which flags were passed."""
    proc, master = _connect(tmp_path, *extra)
    try:
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) >= 0, "SIGTERM killed it without unwinding"
        assert "Logs written:" in proc.stderr.read().decode()
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
