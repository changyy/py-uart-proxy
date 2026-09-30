"""S37: play a recorded session back at its own pace."""

from __future__ import annotations

import asyncio
import io
import os
import signal
import subprocess
import sys
import time
from datetime import datetime

import pytest

from uart_proxy import cli
from uart_proxy.core.recording import load, resolve, sibling
from uart_proxy.ui.replay import play_to_stream


def _record(folder, chunks, *, banner=True, timing=True, base="output"):
    """Write a recording: chunks are (elapsed, bytes)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{base}.log").write_bytes(b"".join(data for _, data in chunks))
    if timing:
        (folder / f"{base}-timing.log").write_text(
            "".join(f"{t:.4f} {len(data)}\n" for t, data in chunks))
    if banner:
        (folder / f"{base}-timestamp.log").write_text(
            "# uart-proxy test · /dev/x @ 115200 8N1\n"
            "# encoding utf-8 · eol cr · started 2026-09-30 11:33:26 +08:00 "
            "(elapsed 0 = this instant)\n")
    return folder / f"{base}.log"


CHUNKS = [(1.0, b"boot\r\n"), (1.5, b"login: "), (10.0, b"root\r\n"), (10.2, b"$ ")]


# ── loading ─────────────────────────────────────────────────────────────────


def test_a_recording_loads_with_its_pace(tmp_path):
    rec = load(str(_record(tmp_path / "s", CHUNKS)))
    assert rec.data == b"boot\r\nlogin: root\r\n$ "
    assert rec.has_timing and rec.times == [1.0, 1.5, 10.0, 10.2]
    assert rec.origin == 1.0 and rec.duration == pytest.approx(9.2)


def test_positions_map_to_bytes(tmp_path):
    rec = load(str(_record(tmp_path / "s", CHUNKS)))
    assert rec.offset_at(0) == 6                  # the first chunk, at position 0
    assert rec.offset_at(0.4) == 6
    assert rec.offset_at(0.5) == 13
    assert rec.offset_at(9.2) == len(rec.data)
    assert rec.next_time_after(0.5) == pytest.approx(9.0)
    assert rec.previous_time_at(5.0) == pytest.approx(0.5)


def test_the_wall_clock_comes_from_the_banner(tmp_path):
    rec = load(str(_record(tmp_path / "s", CHUNKS)))
    assert rec.start_wall == datetime(2026, 9, 30, 11, 33, 26)
    assert rec.wall_at(0) == datetime(2026, 9, 30, 11, 33, 27)   # elapsed 1.0


def test_without_timing_the_bytes_are_still_there(tmp_path):
    rec = load(str(_record(tmp_path / "s", CHUNKS, timing=False)))
    assert not rec.has_timing and rec.data.startswith(b"boot")
    assert rec.offset_at(0) == len(rec.data), "no pace: all at once"


def test_a_timing_file_cut_short_or_overlong_is_made_consistent(tmp_path):
    raw = _record(tmp_path / "s", CHUNKS)
    timing = tmp_path / "s" / "output-timing.log"
    timing.write_text("1.0000 6\ngarbage row\n1.5000 7\n")      # the rest missing
    rec = load(str(raw))
    assert rec.ends[-1] == len(rec.data), "untimed bytes are still played"
    timing.write_text("1.0 6\n1.5 7\n10.0 6\n10.2 999\n")       # claims too much
    assert load(str(raw)).ends[-1] == len(rec.data)


def test_a_part_finds_its_own_timing(tmp_path):
    assert sibling("/x/output.003.log", "timing") == "/x/output-timing.003.log"
    assert sibling("/x/output.log", "timestamp") == "/x/output-timestamp.log"
    raw = _record(tmp_path / "s", CHUNKS, base="output")
    os.replace(raw, tmp_path / "s" / "output.002.log")
    os.replace(tmp_path / "s" / "output-timing.log", tmp_path / "s" / "output-timing.002.log")
    assert load(str(tmp_path / "s" / "output.002.log")).has_timing


def test_resolving_what_to_play(tmp_path):
    older = _record(tmp_path / "store" / "20260930-100000", CHUNKS)
    time.sleep(0.02)
    newer = _record(tmp_path / "store" / "20260930-110000", CHUNKS)
    assert resolve(None, str(tmp_path / "store")) == str(newer)
    assert resolve(str(older.parent), "") == str(older)
    assert resolve(str(older), "") == str(older)
    with pytest.raises(FileNotFoundError):
        resolve(None, str(tmp_path / "empty"))
    with pytest.raises(FileNotFoundError):
        resolve(str(tmp_path / "nope.log"), "")


# ── --no-tui ────────────────────────────────────────────────────────────────


def test_the_stream_player_writes_every_byte_at_the_recorded_pace(tmp_path):
    rec = load(str(_record(tmp_path / "s", CHUNKS)))
    out, sleeps = io.BytesIO(), []
    play_to_stream(rec, out, speed=1.0, max_idle=0, sleep=sleeps.append)
    assert out.getvalue() == rec.data
    assert sleeps == pytest.approx([0.5, 8.5, 0.2])


def test_speed_and_max_idle_shorten_the_waits(tmp_path):
    rec = load(str(_record(tmp_path / "s", CHUNKS)))
    sleeps = []
    play_to_stream(rec, io.BytesIO(), speed=2.0, max_idle=2.0, sleep=sleeps.append)
    assert sleeps == pytest.approx([0.25, 1.0, 0.1]), "8.5 s of silence → 2 s, then ×2"


# ── the TUI player ──────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def _screen(app) -> str:
    return "\n".join(app._term.emulator.display)


def _player(tmp_path, body, chunks=CHUNKS, **kw):
    from uart_proxy.ui.replay import _TEXTUAL_AVAILABLE, ReplayApp

    if not _TEXTUAL_AVAILABLE:
        pytest.skip("textual")
    rec = load(str(_record(tmp_path / "s", chunks)))

    async def scenario():
        clock = Clock()
        app = ReplayApp(rec, clock=clock, **kw)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app._last_tick = clock.now
            await body(app, pilot, clock)

    asyncio.run(scenario())


def _step(app, clock, seconds):
    clock.now += seconds
    app.tick()


def test_it_plays_in_time_and_stops_at_the_end(tmp_path):
    async def body(app, pilot, clock):
        _step(app, clock, 0.1)
        assert "boot" in _screen(app) and "login" not in _screen(app)
        _step(app, clock, 0.5)
        assert "login:" in _screen(app)
        _step(app, clock, 20)
        assert "$" in _screen(app) and not app.playing

    _player(tmp_path, body, max_idle=0)


def test_long_silences_are_cut_short(tmp_path):
    async def body(app, pilot, clock):
        _step(app, clock, 0.6)                 # past "login: ", into 8.5 s of quiet
        for _ in range(4):
            _step(app, clock, 0.5)             # 2 s of it, then it jumps
        _step(app, clock, 0.1)
        assert "root" in _screen(app), "should not have waited out the silence"

    _player(tmp_path, body, max_idle=2.0)


def test_pause_holds_and_seek_goes_both_ways(tmp_path):
    async def body(app, pilot, clock):
        await pilot.press("space")
        assert not app.playing
        _step(app, clock, 30)
        assert app.position == 0.0, "paused means paused"
        await pilot.press("end")
        assert "root" in _screen(app)
        await pilot.press("home")
        assert "root" not in _screen(app) and "boot" in _screen(app), \
            "going back replays from the start into a clean screen"
        await pilot.press("right")
        assert app.position == pytest.approx(5.0)

    _player(tmp_path, body, max_idle=0)


def test_speed_steps_up_and_down(tmp_path):
    async def body(app, pilot, clock):
        await pilot.press("plus")
        assert app.speed == 2.0
        await pilot.press("minus")
        await pilot.press("minus")
        assert app.speed == 0.5
        _step(app, clock, 1.0)
        assert app.position == pytest.approx(0.5)

    _player(tmp_path, body, max_idle=0)


def test_space_at_the_end_plays_again(tmp_path):
    async def body(app, pilot, clock):
        await pilot.press("end")
        app.playing = False
        await pilot.press("space")
        assert app.playing and app.position == 0.0

    _player(tmp_path, body)


def test_the_status_says_where_when_and_how_fast(tmp_path):
    async def body(app, pilot, clock):
        await pilot.press("right")
        text = app.status_text()
        assert "00:00:05.0000 / 00:00:09.2000" in text
        assert "2026-09-30 11:33:32" in text and "×1" in text

    _player(tmp_path, body, max_idle=0)


def test_a_fixed_size_survives_the_window(tmp_path):
    async def body(app, pilot, clock):
        await pilot.resize_terminal(120, 40)
        await pilot.pause()
        assert (app._term.emulator.columns, app._term.emulator.lines) == (80, 24)

    _player(tmp_path, body, size=(80, 24))


def test_a_full_screen_program_replays_as_it_looked(tmp_path):
    """The point: cursor movement, not a log of escape codes."""
    chunks = [(0.0, b"\x1b[2J\x1b[H"), (0.1, b"\x1b[5;10HHELLO"), (0.2, b"\x1b[5;10HWORLD")]

    async def body(app, pilot, clock):
        _step(app, clock, 5)
        lines = app._term.emulator.display
        assert lines[4][9:14] == "WORLD", "drawn over in place, not appended"
        assert "HELLO" not in "\n".join(lines)

    _player(tmp_path, body, chunks=chunks, max_idle=0)


# ── the CLI ─────────────────────────────────────────────────────────────────


def test_replay_of_nothing_says_so(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "DEFAULT_LOG_ROOT", str(tmp_path / "none"))
    assert cli.main(["replay"]) == 1
    assert "no recorded sessions" in capsys.readouterr().err


def test_replay_without_timing_warns(tmp_path, capsys, monkeypatch):
    raw = _record(tmp_path / "s", CHUNKS, timing=False)
    monkeypatch.setattr(sys, "stdout", type("O", (), {"buffer": io.BytesIO()})())
    assert cli.main(["replay", str(raw), "--no-tui"]) == 0
    assert "no timing file" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="needs a pty")
def test_record_then_replay_end_to_end(tmp_path):
    """A real recording, played back with --no-tui: the same bytes come out."""
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    logs = tmp_path / "logs"
    env = dict(os.environ, HOME=str(tmp_path))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port", os.ttyname(slave),
         "--no-tui", "--output-dir", str(logs)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env=env)
    os.close(slave)
    try:
        raw = logs / "output.log"
        deadline = time.monotonic() + 15
        while not raw.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        for part in (b"\x1b[2J\x1b[Hmenu\r\n", b"\x1b[3;1H> option 2", b" [ok]\r\n"):
            os.write(master, part)
            time.sleep(0.25)
        deadline = time.monotonic() + 10
        while b"[ok]" not in raw.read_bytes() and time.monotonic() < deadline:
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)

    timing = (logs / "output-timing.log").read_text().splitlines()
    assert len(timing) >= 2, "the pace was recorded"
    played = subprocess.run(
        [sys.executable, "-m", "uart_proxy", "replay", str(logs), "--no-tui",
         "--speed", "100"], capture_output=True, env=env, timeout=30)
    assert played.returncode == 0, played.stderr
    assert played.stdout == raw.read_bytes()


# ── absolute time, and going to a moment ───────────────────────────────────


from uart_proxy.core.recording import parse_at  # noqa: E402

EPOCH0 = datetime(2026, 9, 30, 3, 0, 0).timestamp()


def _record3(folder, rows, *, banner=False, base="output"):
    """Three-column timing: rows are (epoch, elapsed, bytes)."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{base}.log").write_bytes(b"".join(data for _, _, data in rows))
    (folder / f"{base}-timing.log").write_text(
        "".join(f"{e:.4f} {el:.4f} {len(data)}\n" for e, el, data in rows))
    return folder / f"{base}.log"


ROWS = [(EPOCH0 + 1.0, 1.0, b"boot\r\n"), (EPOCH0 + 1.5, 1.5, b"login: "),
        (EPOCH0 + 760.0, 760.0, b"PANIC\r\n"), (EPOCH0 + 761.0, 761.0, b"reboot\r\n")]


def test_the_recorder_writes_epoch_elapsed_and_bytes(tmp_path):
    from uart_proxy.core.events import Direction, Event, EventKind
    from uart_proxy.core.recorder import Recorder
    from uart_proxy.core.timestamp import TimestampTracker

    tracker = TimestampTracker()
    recorder = Recorder(str(tmp_path))
    stamp = tracker.stamp()
    recorder.handle(Event(EventKind.DATA, Direction.RX, stamp, data=b"hello"))
    recorder.close()
    epoch, elapsed, size = (tmp_path / "output-timing.log").read_text().split()
    assert float(epoch) == pytest.approx(stamp.wall.timestamp(), abs=1e-3)
    assert float(elapsed) == pytest.approx(stamp.elapsed, abs=1e-3)
    assert size == "5"


def test_a_three_column_file_stands_on_its_own(tmp_path):
    """No banner needed: the timing file carries the wall clock itself."""
    rec = load(str(_record3(tmp_path / "s", ROWS)))
    assert rec.epoch_based and rec.start_wall is None
    assert rec.wall_at(0) == datetime(2026, 9, 30, 3, 0, 1)
    assert rec.duration == pytest.approx(760.0)


def test_appended_runs_keep_their_own_pace_and_order(tmp_path):
    """The case elapsed alone got wrong: a second run in the same folder starts
    at elapsed 0 again. On the epoch timeline it simply comes later."""
    rows = [(EPOCH0 + 1, 1.0, b"first\r\n"), (EPOCH0 + 2, 2.0, b"run\r\n"),
            (EPOCH0 + 3600, 0.5, b"second\r\n"), (EPOCH0 + 3610, 10.5, b"run\r\n")]
    rec = load(str(_record3(tmp_path / "s", rows)))
    assert rec.times == sorted(rec.times)
    assert rec.offset_at(3599.0 - 1) == len(b"first\r\nrun\r\n")
    assert rec.offset_at(3609.0 - 1) == len(b"first\r\nrun\r\nsecond\r\n"), \
        "the second run's 10 s gap survived"


def test_the_two_column_format_still_plays(tmp_path):
    rec = load(str(_record(tmp_path / "s", CHUNKS)))
    assert rec.has_timing and not rec.epoch_based
    assert rec.wall_at(0) == datetime(2026, 9, 30, 11, 33, 27), "via the banner"


@pytest.mark.parametrize("text, position", [
    ("+00:12:40", 759.0),                  # elapsed 760 − origin 1
    ("+760", 759.0),
    ("+12:40", 759.0),
    ("03:12:40", 759.0),                    # time of day
    ("2026-09-30 03:12:40", 759.0),         # exact moment
])
def test_at_understands_elapsed_time_of_day_and_moments(tmp_path, text, position):
    rec = load(str(_record3(tmp_path / "s", ROWS)))
    assert parse_at(text, rec) == pytest.approx(position)


def test_at_before_midnight_in_a_recording_that_crosses_it(tmp_path):
    late = datetime(2026, 9, 30, 23, 59, 0).timestamp()
    rows = [(late, 0.0, b"a"), (late + 180, 180.0, b"b")]       # to 00:02
    rec = load(str(_record3(tmp_path / "s", rows)))
    assert parse_at("00:01:00", rec) == pytest.approx(120.0), "the next day"


def test_at_rejects_what_it_cannot_read(tmp_path):
    rec = load(str(_record3(tmp_path / "s", ROWS)))
    with pytest.raises(ValueError, match="not a time"):
        parse_at("soon", rec)


def test_at_needs_a_wall_clock_for_a_time_of_day(tmp_path):
    rec = load(str(_record(tmp_path / "s", CHUNKS, banner=False)))
    with pytest.raises(ValueError, match="no wall-clock"):
        parse_at("03:12:40", rec)
    assert parse_at("+5", rec) == pytest.approx(4.0), "elapsed still works"


def test_the_stream_player_starts_at_a_moment(tmp_path):
    rec = load(str(_record3(tmp_path / "s", ROWS)))
    out, sleeps = io.BytesIO(), []
    play_to_stream(rec, out, max_idle=0, start_at=759.0, sleep=sleeps.append)
    assert out.getvalue() == rec.data, "what came before is drawn at once"
    # 759 s in is the PANIC itself; only "reboot", a second later, is waited for.
    assert sleeps == pytest.approx([1.0]), "then the rest at its pace"


def test_replay_at_through_the_cli(tmp_path, monkeypatch, capsys):
    raw = _record3(tmp_path / "s", ROWS)
    buffer = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", type("O", (), {"buffer": buffer})())
    assert cli.main(["replay", str(raw), "--no-tui", "--at", "+761",
                     "--speed", "1000"]) == 0
    assert buffer.getvalue() == raw.read_bytes()
    assert cli.main(["replay", str(raw), "--no-tui", "--at", "04:00:00"]) == 1
    assert "outside the recording" in capsys.readouterr().err


def test_g_jumps_to_a_time_and_esc_closes_without_quitting(tmp_path):
    from uart_proxy.ui.replay import _TEXTUAL_AVAILABLE, ReplayApp

    if not _TEXTUAL_AVAILABLE:
        pytest.skip("textual")
    rec = load(str(_record3(tmp_path / "s", ROWS)))

    async def scenario():
        app = ReplayApp(rec, clock=Clock(), max_idle=0)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("space")                  # pause, to hold still
            await pilot.press("g")
            await pilot.pause()
            assert app.query_one("#goto").display
            for ch in "03:12:40":
                await pilot.press("colon" if ch == ":" else ch)
            await pilot.press("enter")
            await pilot.pause()
            assert app.position == pytest.approx(759.0)
            assert not app.query_one("#goto").display
            await pilot.press("g")
            await pilot.press("space")                  # typed, not play/pause
            await pilot.press("escape")
            await pilot.pause()
            assert app.is_running, "Esc closed the box; it did not quit"
            assert not app.query_one("#goto").display and not app.playing
            await pilot.press("g")
            for ch in "never":
                await pilot.press(ch)
            await pilot.press("enter")
            await pilot.pause()
            assert any("not a time" in n.message for n in app._notifications)

    asyncio.run(scenario())


def test_the_tui_can_start_at_a_moment(tmp_path):
    from uart_proxy.ui.replay import _TEXTUAL_AVAILABLE, ReplayApp

    if not _TEXTUAL_AVAILABLE:
        pytest.skip("textual")
    rec = load(str(_record3(tmp_path / "s", ROWS)))

    async def scenario():
        app = ReplayApp(rec, clock=Clock(), max_idle=0, start_at=759.5)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "PANIC" in _screen(app) and "reboot" not in _screen(app)

    asyncio.run(scenario())
