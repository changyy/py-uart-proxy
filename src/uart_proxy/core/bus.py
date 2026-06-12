"""
A tiny thread-safe publish/subscribe bus.

The session publishes :class:`~uart_proxy.core.events.Event` objects from its
read thread; subscribers (recorder, plugins, proxy server, UI) receive them
synchronously in the publishing thread. Subscriber callbacks must therefore be
quick and must not block — anything heavy (socket writes, UI updates) should
hand off to its own queue/loop.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from .events import Event

logger = logging.getLogger(__name__)

Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._subscribers: list[Subscriber] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Subscriber) -> Callable[[], None]:
        """Register a callback. Returns a function that unsubscribes it."""
        with self._lock:
            self._subscribers.append(callback)

        def _unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return _unsubscribe

    def publish(self, event: Event) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:  # noqa: BLE001 - never let one subscriber break others
                logger.exception("Error in event subscriber %r", callback)
