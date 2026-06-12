"""S12: auto-reconnect — wait for an absent device, recover from a drop."""

from __future__ import annotations

import time

from uart_proxy.core.events import Direction, EventKind
from uart_proxy.core.session import UartSession

from conftest import FakeSource


def _statuses(events):
    return [e.text for e in events if e.kind == EventKind.STATUS]


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_waits_for_absent_device_then_connects():
    # open() fails twice (device not plugged), then succeeds.
    source = FakeSource(fail_opens=2)
    session = UartSession(source, reconnect_interval=0.05)
    events = []
    session.bus.subscribe(events.append)

    session.start()
    # start() must NOT block on the missing device.
    assert _wait_for(lambda: "waiting" in _statuses(events))
    assert _wait_for(lambda: session.is_connected)

    source.feed(b"alive\n")
    assert _wait_for(
        lambda: any(
            e.kind == EventKind.LINE and e.text == "alive" for e in events
        )
    )
    session.stop()
    assert "connected" in _statuses(events)


def test_recovers_after_device_drop():
    source = FakeSource()
    session = UartSession(source, reconnect_interval=0.05)
    events = []
    session.bus.subscribe(events.append)

    session.start()
    assert _wait_for(lambda: session.is_connected)
    source.feed(b"before\n")
    assert _wait_for(lambda: any(e.text == "before" for e in events if e.kind == EventKind.LINE))

    # Device disappears -> read() raises -> manager reconnects -> reattaches.
    source.drop()
    assert _wait_for(lambda: "reconnecting" in _statuses(events))
    assert _wait_for(lambda: session.is_connected)

    source.feed(b"after\n")
    assert _wait_for(lambda: any(e.text == "after" for e in events if e.kind == EventKind.LINE))
    session.stop()


def test_no_reconnect_when_disabled():
    source = FakeSource(fail_opens=99)
    session = UartSession(source, auto_reconnect=False, reconnect_interval=0.05)
    events = []
    session.bus.subscribe(events.append)
    session.start()
    assert _wait_for(lambda: "waiting" in _statuses(events))
    # With reconnect disabled, the manager gives up; never connects.
    time.sleep(0.2)
    assert not session.is_connected
    session.stop()


def test_write_while_disconnected_raises():
    source = FakeSource(fail_opens=99)
    session = UartSession(source, auto_reconnect=False, reconnect_interval=0.05)
    session.start()
    time.sleep(0.1)
    try:
        raised = False
        try:
            session.write(b"AT")
        except RuntimeError:
            raised = True
        assert raised
    finally:
        session.stop()
