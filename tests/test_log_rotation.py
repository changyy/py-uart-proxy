"""S32: a session that runs for days is split into parts, and can be capped."""

from __future__ import annotations

import os

import pytest

from uart_proxy import cli
from uart_proxy.cli import close_recorder
from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.recorder import Recorder
from uart_proxy.core.timestamp import TimestampTracker

STAMP = TimestampTracker()


def _feed(recorder, text: str):
    data = (text + "\r\n").encode()
    recorder.handle(Event(EventKind.DATA, Direction.RX, STAMP.stamp(), data=data))
    recorder.handle(Event(EventKind.LINE, Direction.RX, STAMP.stamp(), text=text))


def _files(directory) -> list[str]:
    return sorted(os.listdir(directory))


def test_off_by_default(tmp_path):
    recorder = Recorder(str(tmp_path))
    for i in range(2000):
        _feed(recorder, f"line {i:05d} " + "x" * 40)
    recorder.close()
    assert _files(tmp_path) == ["output-fulltimestamp.log", "output-timestamp.log",
                                "output.log"]


def test_passing_the_limit_splits_all_three_files_together(tmp_path):
    recorder = Recorder(str(tmp_path), rotate_bytes=4096)
    for i in range(200):
        _feed(recorder, f"line {i:05d} " + "x" * 40)
    parts = list(recorder.parts)
    recorder.close()
    assert parts, "should have rotated"
    assert parts[0] == [str(tmp_path / "output.001.log"),
                        str(tmp_path / "output-timestamp.001.log"),
                        str(tmp_path / "output-fulltimestamp.001.log")]
    assert all(os.path.getsize(p) < 3 * 4096 for part in parts for p in part)


def test_nothing_is_lost_across_parts(tmp_path):
    recorder = Recorder(str(tmp_path), rotate_bytes=2048)
    sent = [f"line {i:05d}" for i in range(500)]
    for text in sent:
        _feed(recorder, text)
    parts = list(recorder.parts)
    recorder.close()
    raw_files = [part[0] for part in parts] + [str(tmp_path / "output.log")]
    raw = b"".join(open(p, "rb").read() for p in raw_files)
    assert raw == "".join(t + "\r\n" for t in sent).encode(), "raw is byte-exact"
    text_files = [part[1] for part in parts] + [str(tmp_path / "output-timestamp.log")]
    rows = [line for p in text_files for line in open(p).read().splitlines()
            if line.startswith("[")]
    assert [row.split("] ", 1)[1] for row in rows] == sent, "whole lines, in order"


def test_each_part_reads_on_its_own(tmp_path):
    recorder = Recorder(str(tmp_path), rotate_bytes=2048)
    recorder.set_banner(["uart-proxy test · /dev/x @ 115200 8N1"])
    for i in range(200):
        _feed(recorder, f"line {i:05d}")
    recorder.close()
    first = (tmp_path / "output-timestamp.001.log").read_text().splitlines()
    second = (tmp_path / "output-timestamp.002.log").read_text().splitlines()
    assert first[0] == "# uart-proxy test · /dev/x @ 115200 8N1"
    assert first[-1] == "# continues in part 2"
    assert second[0] == "# uart-proxy test · /dev/x @ 115200 8N1"
    assert second[1].startswith("# part 2 — part 1 is output.001.log")


def test_the_raw_log_never_gets_a_banner_even_in_a_later_part(tmp_path):
    recorder = Recorder(str(tmp_path), rotate_bytes=1024)
    recorder.set_banner(["banner"])
    for i in range(100):
        _feed(recorder, f"line {i:05d}")
    recorder.close()
    for name in _files(tmp_path):
        if name.startswith("output.") and name.endswith(".log"):
            assert b"#" not in (tmp_path / name).read_bytes()


def test_keep_parts_caps_the_session(tmp_path):
    recorder = Recorder(str(tmp_path), rotate_bytes=1024, keep_parts=2)
    for i in range(1000):
        _feed(recorder, f"line {i:05d}")
    assert len(recorder.parts) == 2
    newest = recorder.parts[-1][0]
    recorder.close()
    raw_parts = [n for n in _files(tmp_path)
                 if n.startswith("output.") and n.count(".") == 2]
    assert len(raw_parts) == 2
    assert os.path.basename(newest) in raw_parts
    assert "output.001.log" not in raw_parts, "the oldest go first"


def test_a_line_is_never_split_across_parts(tmp_path):
    """Rotation waits for the line to finish, even past the limit."""
    recorder = Recorder(str(tmp_path), rotate_bytes=100)
    long_line = "y" * 150
    _feed(recorder, long_line)
    recorder.close()
    rows = [line for line in (tmp_path / "output-timestamp.001.log").read_text()
            .splitlines() if line.startswith("[")]
    assert rows and rows[0].endswith(long_line)


def test_a_stream_without_newlines_still_rotates(tmp_path):
    recorder = Recorder(str(tmp_path), rotate_bytes=1000)
    for _ in range(30):
        recorder.handle(Event(EventKind.DATA, Direction.RX, STAMP.stamp(),
                              data=b"\x00" * 100))
    parts = list(recorder.parts)
    recorder.close()
    assert parts, "binary data never ends a line; it must rotate anyway"


def test_raw_only_recording_rotates_at_the_limit(tmp_path):
    recorder = Recorder(str(tmp_path), relative=False, full=False, rotate_bytes=1000)
    for _ in range(12):
        recorder.handle(Event(EventKind.DATA, Direction.RX, STAMP.stamp(),
                              data=b"z" * 100))
    parts = list(recorder.parts)
    recorder.close()
    assert parts and parts[0] == [str(tmp_path / "output.001.log")]


def test_appending_to_a_folder_never_overwrites_an_earlier_runs_parts(tmp_path):
    first = Recorder(str(tmp_path), rotate_bytes=1024)
    for i in range(100):
        _feed(first, f"first {i:05d}")
    first_parts = [p for part in first.parts for p in part]
    first.close()
    before = {p: open(p, "rb").read() for p in first_parts}

    second = Recorder(str(tmp_path), rotate_bytes=1024, append=True)
    for i in range(100):
        _feed(second, f"second {i:05d}")
    second.close()
    for path, content in before.items():
        assert open(path, "rb").read() == content, f"{path} was overwritten"


def test_logs_written_lists_every_part(tmp_path):
    recorder = Recorder(str(tmp_path), rotate_bytes=1024)
    for i in range(100):
        _feed(recorder, f"line {i:05d}")
    written = close_recorder(recorder)
    assert str(tmp_path / "output.001.log") in written
    assert str(tmp_path / "output.log") in written
    assert all(os.path.exists(p) for p in written)


def test_the_flags():
    args = cli.build_parser().parse_args(
        ["connect", "--port", "/dev/x", "--log-rotate-mb", "50", "--log-keep-parts", "4"])
    assert args.log_rotate_mb == 50 and args.log_keep_parts == 4
    defaults = cli.build_parser().parse_args(["connect", "--port", "/dev/x"])
    assert defaults.log_rotate_mb == 0 and defaults.log_keep_parts == 0


@pytest.mark.skipif(os.name != "posix", reason="needs a pty")
def test_a_real_session_with_rotation(tmp_path):
    import pty
    import signal
    import subprocess
    import sys
    import time
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    logs = tmp_path / "logs"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port", os.ttyname(slave),
         "--no-tui", "--output-dir", str(logs), "--log-rotate-mb", "0.01",
         "--log-keep-parts", "2"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        env=dict(os.environ, HOME=str(tmp_path)))
    os.close(slave)
    try:
        deadline = time.monotonic() + 15
        while not (logs / "output.log").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        deadline = time.monotonic() + 15
        i = 0
        while not (logs / "output.003.log").exists() and time.monotonic() < deadline:
            for _ in range(50):
                os.write(master, f"boot line {i:05d} ................\r\n".encode())
                i += 1
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        err = proc.communicate(timeout=10)[1].decode()
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    names = _files(logs)
    assert "output.003.log" in names
    assert "output.001.log" not in names, "--log-keep-parts 2 removed the oldest"
    assert "output.003.log" in err, "Logs written: lists the parts"
