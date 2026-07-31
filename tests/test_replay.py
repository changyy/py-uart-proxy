"""S18: replay on attach — seeing what happened while nobody was watching."""

from __future__ import annotations

import time

import pytest

from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.replay import (
    DEFAULT_REPLAY_LINES,
    ReplayBuffer,
    ReplayEntry,
    describe,
)
from uart_proxy.core.session import UartSession
from uart_proxy.core.timestamp import TimestampTracker

from conftest import FakeSource


def _line(text: str, direction=Direction.RX, kind=EventKind.LINE, seq=1) -> Event:
    return Event(kind, direction, TimestampTracker().stamp(),
                 seq=seq, data=text.encode(), text=text)


# ── the buffer ──────────────────────────────────────────────────────────────


def test_it_keeps_assembled_device_output():
    buf = ReplayBuffer()
    buf.handle(_line("login:"))
    buf.handle(_line("root@target:~#"))
    assert [e.text for e in buf.snapshot()] == ["login:", "root@target:~#"]


def test_it_keeps_the_original_timestamps():
    """The whole reason it stores events and not bytes: a byte buffer would make
    every replayed line look like it happened at the moment you attached."""
    buf = ReplayBuffer()
    event = _line("early")
    buf.handle(event)
    time.sleep(0.02)
    entry = buf.snapshot()[0]
    assert entry.elapsed == pytest.approx(event.stamp.elapsed)
    assert entry.wall == event.stamp.wall_str()


@pytest.mark.parametrize(
    "event, why",
    [
        (_line("typed", direction=Direction.TX), "TX is the operator's own past"),
        (_line("raw", kind=EventKind.DATA), "DATA would duplicate the LINEs"),
        (Event(EventKind.NOTICE, Direction.SYS, TimestampTracker().stamp(),
               text="grep hit"), "a notice is not device output"),
        (Event(EventKind.STATUS, Direction.SYS, TimestampTracker().stamp(),
               text="connected"), "a stale banner would mislead"),
    ],
)
def test_it_keeps_only_what_the_device_said(event, why):
    buf = ReplayBuffer()
    buf.handle(event)
    assert buf.snapshot() == [], why


def test_it_is_bounded_and_keeps_the_newest():
    buf = ReplayBuffer(max_lines=3)
    for i in range(10):
        buf.handle(_line(f"line {i}"))
    assert [e.text for e in buf.snapshot()] == ["line 7", "line 8", "line 9"]
    assert len(buf) == 3


def test_zero_lines_disables_it():
    buf = ReplayBuffer(max_lines=0)
    buf.handle(_line("nope"))
    assert not buf.enabled
    assert buf.snapshot() == []


def test_a_client_can_ask_for_fewer_than_are_held():
    buf = ReplayBuffer()
    for i in range(10):
        buf.handle(_line(f"line {i}"))
    assert [e.text for e in buf.snapshot(3)] == ["line 7", "line 8", "line 9"]
    assert buf.snapshot(0) == []
    assert len(buf.snapshot(999)) == 10


def test_the_default_window_is_generous_but_bounded():
    assert 100 <= DEFAULT_REPLAY_LINES <= 100_000


def test_entries_survive_the_wire_format():
    original = ReplayEntry(seq=7, wall="2026-07-31 19:09:52", elapsed=1.5, text="hi")
    assert ReplayEntry.from_message(original.to_message()) == original


def test_a_malformed_message_does_not_explode():
    assert ReplayEntry.from_message({}).text == ""


def test_the_divider_summarises_the_span():
    entries = [
        ReplayEntry(1, "2026-07-31 10:00:00", 0.0, "a"),
        ReplayEntry(2, "2026-07-31 10:02:30", 150.0, "b"),
    ]
    summary = describe(entries)
    assert "2 lines" in summary and "2m30s" in summary
    assert describe([]) == "no history"


# ── the shared timeline ─────────────────────────────────────────────────────


def test_rebasing_adopts_another_sessions_clock():
    """Without this, replayed history is measured from the server's start and
    live output from this client's connect, so the elapsed column jumps back."""
    tracker = TimestampTracker()
    assert tracker.stamp().elapsed < 1.0
    tracker.rebase(3600.0)
    assert tracker.stamp().elapsed == pytest.approx(3600.0, abs=1.0)


def test_rebasing_keeps_time_moving_forwards():
    tracker = TimestampTracker()
    tracker.rebase(100.0)
    first = tracker.stamp()
    time.sleep(0.01)
    assert tracker.stamp().elapsed > first.elapsed


def test_a_nonsense_rebase_is_ignored():
    tracker = TimestampTracker()
    before = tracker.stamp().elapsed
    tracker.rebase(-5.0)
    assert tracker.stamp().elapsed >= before


# ── wired to a session ──────────────────────────────────────────────────────


def test_it_fills_from_a_live_session(fake_source):
    """Subscribed like the recorder, so history exists from the moment the
    session starts — not from whenever a client happens to connect."""
    session = UartSession(fake_source, auto_reconnect=False)
    buf = ReplayBuffer()
    session.bus.subscribe(buf.handle)
    session.start()
    try:
        deadline = time.monotonic() + 5
        fake_source.feed(b"one\ntwo\n")
        while time.monotonic() < deadline and len(buf) < 2:
            time.sleep(0.01)
        assert [e.text for e in buf.snapshot()] == ["one", "two"]
    finally:
        session.stop()


def test_the_operators_own_writes_are_not_replayed(fake_source):
    session = UartSession(fake_source, auto_reconnect=False)
    buf = ReplayBuffer()
    session.bus.subscribe(buf.handle)
    session.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not session.is_connected:
            time.sleep(0.01)
        session.send_text("reboot")
        time.sleep(0.3)
        assert buf.snapshot() == [], "replay is what the device said, not what we typed"
    finally:
        session.stop()
