"""
Headless runner.

Streams the session to stdout without a TUI — ideal for a server box that only
needs to expose the proxy and write log files, or for piping output elsewhere.
Honours the same timestamp display modes as the TUI.
"""

from __future__ import annotations

import sys
import threading

from ..core.events import Direction, Event, EventKind
from ..core.session import UartSession

_TS_NONE, _TS_REL, _TS_FULL = "none", "relative", "full"


def _prefix(event: Event, ts_mode: str) -> str:
    if ts_mode == _TS_REL:
        return f"[{event.stamp.elapsed_str()}] "
    if ts_mode == _TS_FULL:
        return f"[{event.stamp.wall_str()} | {event.stamp.elapsed_str()}] "
    return ""


def run_headless(session: UartSession, *, ts_mode: str = _TS_REL) -> None:
    """Start the session and print its line stream until interrupted."""
    stop = threading.Event()

    def on_event(event: Event) -> None:
        if event.kind == EventKind.LINE and event.direction == Direction.RX:
            sys.stdout.write(f"{_prefix(event, ts_mode)}{event.text}\n")
            sys.stdout.flush()
        elif event.kind == EventKind.NOTICE:
            sys.stdout.write(f"\033[33m* {event.text}\033[0m\n")
            sys.stdout.flush()
        elif event.kind == EventKind.STATUS:
            sys.stdout.write(f"\033[36m# {event.text} {event.meta or ''}\033[0m\n")
            sys.stdout.flush()
            if event.text in ("disconnected", "error"):
                stop.set()

    unsubscribe = session.bus.subscribe(on_event)
    try:
        session.start()
        while not stop.is_set():
            stop.wait(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        unsubscribe()
        session.stop()
