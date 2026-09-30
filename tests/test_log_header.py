"""S25: a recording says what it is, and when elapsed 0 was."""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import time

import pytest

from uart_proxy import __version__
from uart_proxy.cli import session_footer, session_header
from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.recorder import Recorder
from uart_proxy.core.session import UartSession
from uart_proxy.core.timestamp import TimestampTracker

from conftest import FakeSource


def _rx_line(text: str) -> Event:
    return Event(EventKind.LINE, Direction.RX, TimestampTracker().stamp(), text=text)


def _rx_data(data: bytes) -> Event:
    return Event(EventKind.DATA, Direction.RX, TimestampTracker().stamp(), data=data)


# ── Recorder.mark ───────────────────────────────────────────────────────────


def test_a_mark_goes_to_the_timestamped_files_only(tmp_path):
    recorder = Recorder(str(tmp_path))
    recorder.mark("uart-proxy test · /dev/x")
    recorder.handle(_rx_data(b"hello\r\n"))
    recorder.handle(_rx_line("hello"))
    recorder.close()
    assert (tmp_path / "output.log").read_bytes() == b"hello\r\n", \
        "the raw log is the device's bytes and nothing else"
    for name in ("output-timestamp.log", "output-fulltimestamp.log"):
        lines = (tmp_path / name).read_text().splitlines()
        assert lines[0] == "# uart-proxy test · /dev/x"
        assert lines[1].startswith("[") and lines[1].endswith("] hello")


def test_a_mark_can_never_look_like_a_device_line(tmp_path):
    """Device lines always start `[stamp]`; marks always start `# `."""
    recorder = Recorder(str(tmp_path))
    recorder.mark("x")
    recorder.handle(_rx_line("# looks like a comment"))
    recorder.close()
    lines = (tmp_path / "output-timestamp.log").read_text().splitlines()
    assert lines[0] == "# x"
    assert lines[1].startswith("[") and lines[1].endswith("] # looks like a comment")


def test_marks_are_skipped_for_files_not_recorded(tmp_path):
    recorder = Recorder(str(tmp_path), relative=False, full=False)
    recorder.mark("nothing to write to")
    recorder.close()
    assert sorted(os.listdir(tmp_path)) == ["output-timing.log", "output.log"]


# ── what the header and footer say ──────────────────────────────────────────


def _session(**kw):
    return UartSession(FakeSource(), encoding=kw.get("encoding", "latin-1"),
                       default_eol=kw.get("eol", b"\r"))


def test_the_header_names_version_source_settings_and_the_origin():
    session = _session()
    lines = session_header(session, argparse.Namespace())
    text = "\n".join(lines)
    assert f"uart-proxy {__version__}" in text
    assert session.source.description() in text
    assert "encoding latin-1" in text and "eol cr" in text
    start = session.tracker.start_wall.strftime("%Y-%m-%d %H:%M:%S")
    assert f"started {start}" in text
    assert re.search(r"started \S+ \S+ [+-]\d\d:\d\d", text), "with its UTC offset"


def test_the_footer_gives_the_window_in_both_axes():
    session = _session()
    footer = session_footer(session)
    assert footer.startswith("ended · ")
    assert " ~ " in footer and "00:00:00.0000 ~ 00:00:" in footer


def test_the_header_follows_an_adopted_remote_timeline():
    """After `remote`/`attach` rebases onto the server's clock, the header's
    origin is the server's — so elapsed values match the server's logs."""
    session = _session()
    session.tracker.rebase(3600.0)
    text = "\n".join(session_header(session, argparse.Namespace()))
    start = session.tracker.start_wall.strftime("%Y-%m-%d %H:%M:%S")
    assert f"started {start}" in text


# ── end to end ──────────────────────────────────────────────────────────────


@pytest.mark.skipif(os.name != "posix", reason="needs a pty")
def test_a_real_recording_opens_and_closes_with_its_banner(tmp_path):
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    logs = tmp_path / "logs"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port", os.ttyname(slave),
         "--no-tui", "--output-dir", str(logs), "--baud", "9600"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        env=dict(os.environ, HOME=str(tmp_path)))
    os.close(slave)
    try:
        raw = logs / "output.log"
        deadline = time.monotonic() + 15
        while not raw.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)  # connected, not just created
        os.write(master, b"boot ok\r\n")
        deadline = time.monotonic() + 10
        while b"boot ok" not in raw.read_bytes() and time.monotonic() < deadline:
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) >= 0
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)

    assert raw.read_bytes() == b"boot ok\r\n"
    lines = (logs / "output-fulltimestamp.log").read_text().splitlines()
    assert lines[0].startswith(f"# uart-proxy {__version__} · ")
    assert "@ 9600 8N1" in lines[0]
    assert lines[1].startswith("# encoding utf-8 · eol cr · started ")
    assert any(line.endswith("] boot ok") for line in lines[2:-1])
    assert lines[-1].startswith("# ended · ")
