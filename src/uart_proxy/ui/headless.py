"""
Headless runner.

Streams the session to stdout without a TUI — ideal for a server box that only
needs to expose the proxy and write log files, or for piping output elsewhere.
Honours the same timestamp display modes as the TUI.
"""

from __future__ import annotations

import os
import signal
import sys
import threading

from typing import Callable, Optional

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


def follow_terminal_size(session: UartSession) -> Optional[Callable[[], None]]:
    """Tell a transport that can take a window size (ssh, SPEC S35) the size
    of the terminal we print to, now and on every SIGWINCH.

    Nothing to do without one — a daemon's stdout is /dev/null — or when the
    source has no use for it. Returns what restores the previous handler.
    """
    tell = getattr(session.source, "set_window_size", None)
    if tell is None or not sys.stdout.isatty():
        return None

    def apply(*_signal) -> None:
        try:
            size = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            return
        tell(size.columns, size.lines)

    apply()  # before the session starts, so the far end's first look is right
    if (not hasattr(signal, "SIGWINCH")
            or threading.current_thread() is not threading.main_thread()):
        return None
    previous = signal.signal(signal.SIGWINCH, apply)
    return lambda: signal.signal(signal.SIGWINCH, previous)


def run_headless(session: UartSession, *, ts_mode: str = _TS_REL,
                 quiet: bool = False, history: Optional[list] = None,
                 terminate: Optional[threading.Event] = None) -> None:
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
            # Only the session's end: an `error` is a dropped device that the
            # session is about to reconnect (S12). Ending on it made every
            # background session exit on the first unplug. `disconnected` is
            # always published when a session really ends — stop(), giving up
            # without --reconnect, or a refusal.
            if event.text == "disconnected":
                stop.set()

    unsubscribe = session.bus.subscribe(on_event)
    restore_winch = follow_terminal_size(session)
    try:
        session.start()
        # ``terminate``: a SIGTERM, recorded rather than raised (SPEC S16).
        while not stop.is_set() and not (terminate is not None and terminate.is_set()):
            stop.wait(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        # Stop first, so the closing "disconnected" status is still printed;
        # unsubscribing before it made the shutdown happen invisibly.
        session.stop()
        unsubscribe()
        if restore_winch is not None:
            restore_winch()
