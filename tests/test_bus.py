"""S5/S7: event bus pub/sub and subscriber isolation."""

from __future__ import annotations

from uart_proxy.core.bus import EventBus
from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.timestamp import TimestampTracker


def _event() -> Event:
    return Event(
        kind=EventKind.NOTICE,
        direction=Direction.SYS,
        stamp=TimestampTracker().stamp(),
        text="hi",
    )


def test_subscribe_receives_and_unsubscribe_stops():
    bus = EventBus()
    seen = []
    unsubscribe = bus.subscribe(seen.append)
    bus.publish(_event())
    assert len(seen) == 1
    unsubscribe()
    bus.publish(_event())
    assert len(seen) == 1


def test_one_bad_subscriber_does_not_break_others():
    bus = EventBus()
    seen = []

    def boom(_event):
        raise RuntimeError("boom")

    bus.subscribe(boom)
    bus.subscribe(seen.append)
    bus.publish(_event())  # must not raise
    assert len(seen) == 1
