"""
Headless runner.

Streams the session to stdout without a TUI — ideal for a server box that only
needs to expose the proxy and write log files, or for piping output elsewhere.
Honours the same timestamp display modes as the TUI.
"""

from __future__ import annotations

import sys
import threading

from typing import Optional

from ..core.events import Direction, Event, EventKind
from ..core.replay import describe
from ..core.timestamp import format_elapsed
from ..core.session import UartSession

_TS_NONE, _TS_REL, _TS_FULL = "none", "relative", "full"


def _prefix(event: Event, ts_mode: str) -> str:
    if ts_mode == _TS_REL:
        return f"[{event.stamp.elapsed_str()}] "
    if ts_mode == _TS_FULL:
        return f"[{event.stamp.wall_str()} | {event.stamp.elapsed_str()}] "
    return ""


def _print_history(entries: list, ts_mode: str) -> None:
    """Print replayed lines, then a divider, so history can't read as live.

    The stamps are the server's, from when each line actually arrived — which is
    the whole reason replay carries events rather than bytes.
    """
    sys.stdout.write(f"\033[2m── replayed {describe(entries)} ──\033[0m\n")
    for entry in entries:
        if ts_mode == _TS_FULL:
            prefix = f"[{entry.wall} | {format_elapsed(entry.elapsed)}] "
        elif ts_mode == _TS_REL:
            prefix = f"[{format_elapsed(entry.elapsed)}] "
        else:
            prefix = ""
        sys.stdout.write(f"\033[2m{prefix}{entry.text}\033[0m\n")
    sys.stdout.write("\033[2m── live ──\033[0m\n")
    sys.stdout.flush()


def run_headless(session: UartSession, *, ts_mode: str = _TS_REL,
                 quiet: bool = False, history: Optional[list] = None) -> None:
    """Start the session and print its line stream until interrupted.

    ``quiet`` drops the data stream and keeps only notices and status, on
    **stderr** — what a detached daemon wants: the traffic itself already goes to
    the recorder's log files, so duplicating it into a second one is waste, while
    the notices are how you diagnose a daemon that isn't doing what you expected.
    """
    if history:
        _print_history(history, ts_mode)

    stop = threading.Event()
    # In quiet mode stdout is /dev/null (the daemon detached it), so the messages
    # worth keeping have to go to the channel that was kept.
    meta_out = sys.stderr if quiet else sys.stdout

    def emit(stream, text: str) -> None:
        stream.write(text)
        stream.flush()

    def on_event(event: Event) -> None:
        if event.kind == EventKind.LINE and event.direction == Direction.RX:
            if not quiet:
                emit(sys.stdout, f"{_prefix(event, ts_mode)}{event.text}\n")
        elif event.kind == EventKind.NOTICE:
            emit(meta_out, f"\033[33m* {event.text}\033[0m\n")
        elif event.kind == EventKind.STATUS:
            emit(meta_out, f"\033[36m# {event.text} {event.meta or ''}\033[0m\n")
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
        # Stop first, so the closing "disconnected" status is still printed;
        # unsubscribing before it made the shutdown happen invisibly.
        session.stop()
        unsubscribe()
