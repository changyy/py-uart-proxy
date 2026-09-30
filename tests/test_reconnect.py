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


def test_giving_up_is_announced_once():
    """With reconnect off, the manager ending on its own must say so — headless
    mode waits for `disconnected` and would otherwise never exit."""
    source = FakeSource(fail_opens=99)
    session = UartSession(source, auto_reconnect=False, reconnect_interval=0.05)
    events = []
    session.bus.subscribe(events.append)
    session.start()
    assert _wait_for(lambda: "disconnected" in _statuses(events))
    assert not session.is_running
    session.stop()  # already over: must not announce it a second time
    assert _statuses(events).count("disconnected") == 1


def test_a_long_wait_is_one_line_not_one_per_attempt():
    source = FakeSource(fail_opens=99)
    session = UartSession(source, reconnect_interval=0.01)
    events = []
    session.bus.subscribe(events.append)
    session.start()
    assert _wait_for(lambda: source.open_calls >= 10)
    session.stop()
    assert _statuses(events).count("waiting") == 1


class _ChangingReasons(FakeSource):
    """Absent, then busy, then absent again — each change is worth a line."""

    REASONS = ["not found", "not found", "busy", "busy", "not found"]

    def open(self) -> None:
        self.open_calls += 1
        if self.open_calls <= len(self.REASONS):
            raise IOError(self.REASONS[self.open_calls - 1])
        self.opened = True


def test_waiting_is_repeated_when_the_reason_changes():
    source = _ChangingReasons()
    session = UartSession(source, reconnect_interval=0.01)
    events = []
    session.bus.subscribe(events.append)
    session.start()
    assert _wait_for(lambda: session.is_connected)
    session.stop()
    waits = [e.meta["error"] for e in events
             if e.kind == EventKind.STATUS and e.text == "waiting"]
    assert waits == ["not found", "busy", "not found"]


def test_a_wait_after_a_drop_is_reported_afresh():
    """A connection in between starts a new wait, even for the same reason."""
    source = FakeSource(fail_opens=1)
    session = UartSession(source, reconnect_interval=0.01)
    events = []
    session.bus.subscribe(events.append)
    session.start()
    assert _wait_for(lambda: session.is_connected)
    source.fail_opens = source.open_calls + 1  # the next open fails once more
    source.drop()
    assert _wait_for(lambda: _statuses(events).count("waiting") == 2)
    session.stop()
