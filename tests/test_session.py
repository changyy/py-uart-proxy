"""S5: the session pipeline."""

from __future__ import annotations

import time

from uart_proxy.core.events import Direction, EventKind
from uart_proxy.core.session import UartSession

from conftest import FakeSource


def _collect(session: UartSession) -> list:
    events = []
    session.bus.subscribe(events.append)
    return events


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_rx_produces_data_and_line_events():
    source = FakeSource()
    session = UartSession(source)
    events = _collect(session)
    session.start()
    source.feed(b"one\ntwo\n")

    assert _wait_for(
        lambda: len([e for e in events if e.kind == EventKind.LINE and e.direction == Direction.RX]) >= 2
    )
    session.stop()

    rx_lines = [e.text for e in events if e.kind == EventKind.LINE and e.direction == Direction.RX]
    assert rx_lines[:2] == ["one", "two"]
    assert any(e.kind == EventKind.DATA and e.direction == Direction.RX for e in events)


def test_send_text_appends_eol_and_writes():
    source = FakeSource()
    session = UartSession(source, default_eol=b"\r\n")
    session.start()
    assert _wait_for(lambda: session.is_connected)
    n = session.send_text("AT")
    session.stop()
    assert source.writes == [b"AT\r\n"]
    assert n == 4


def test_idle_flush_emits_partial_line():
    source = FakeSource()
    session = UartSession(source)
    events = _collect(session)
    session.start()
    source.feed(b"login: ")  # no newline

    assert _wait_for(
        lambda: any(
            e.kind == EventKind.LINE and e.text == "login: " for e in events
        )
    )
    session.stop()


def test_status_events_on_start_and_stop():
    source = FakeSource()
    session = UartSession(source)
    events = _collect(session)
    session.start()
    assert _wait_for(lambda: session.is_connected)
    session.stop()
    statuses = [e.text for e in events if e.kind == EventKind.STATUS]
    assert "connected" in statuses
    assert "disconnected" in statuses
    assert source.open_calls >= 1 and source.closed
