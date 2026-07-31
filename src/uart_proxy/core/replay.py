"""
Recent history, so a client that attaches can see what it missed.

A background session (S17) keeps running with nobody watching. When you come
back, a live-only stream is close to useless: a serial console is often quiet, so
you get a blank screen and no way to tell "attached and idle" from "broken", let
alone what the device said while you were away.

:class:`ReplayBuffer` is a bounded tail of the session's output, kept as
**events** rather than bytes. That distinction is the whole point: an event
carries the :class:`~uart_proxy.core.timestamp.Stamp` from when the line actually
arrived, so replayed output can be shown on the same two time axes as everything
else. A byte buffer would lose exactly that, and every replayed line would appear
to have happened at the moment you attached — which is worse than no history,
because it is history that lies about when it happened.

It is bounded on purpose. Nobody wants two hours of boot logs poured into a fresh
view, and the complete record is already on disk in the recorder's files; this is
the "what just happened" window, not the archive.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Optional

from .events import Direction, Event, EventKind

#: Lines of history a client gets by default. A few thousand is enough to see
#: what a device has been up to without burying the live tail underneath it.
DEFAULT_REPLAY_LINES = 2000


@dataclass(frozen=True)
class ReplayEntry:
    """One historical line, with the time it really arrived."""

    seq: int
    wall: str        # server-side wall clock, "%Y-%m-%d %H:%M:%S"
    elapsed: float   # seconds since the server's session started
    text: str

    def to_message(self) -> dict:
        return {
            "type": "replay",
            "seq": self.seq,
            "wall": self.wall,
            "elapsed": round(self.elapsed, 4),
            "text": self.text,
        }

    @classmethod
    def from_message(cls, msg: dict) -> "ReplayEntry":
        return cls(
            seq=int(msg.get("seq", 0)),
            wall=str(msg.get("wall", "")),
            elapsed=float(msg.get("elapsed", 0.0)),
            text=str(msg.get("text", "")),
        )


class ReplayBuffer:
    """A bounded tail of assembled device output, as an EventBus sink.

    Subscribed alongside the recorder and the proxy. It keeps ``LINE(RX)`` events
    — already assembled, already stamped — so handing them to a client needs no
    re-parsing and no re-stamping.

    Deliberately not kept: TX, notices and status. Replay answers "what did the
    device say while I was away", and mixing in the operator's own past keystrokes
    or a stale `connected` banner would confuse that.
    """

    def __init__(self, max_lines: int = DEFAULT_REPLAY_LINES) -> None:
        self.max_lines = max_lines
        self._entries: "deque[ReplayEntry]" = deque(maxlen=max(0, max_lines) or None)
        self._lock = threading.Lock()
        self._enabled = max_lines > 0

    # ── EventBus sink ──────────────────────────────────────────────────────

    def handle(self, event: Event) -> None:
        if not self._enabled:
            return
        if event.kind is not EventKind.LINE or event.direction is not Direction.RX:
            return
        entry = ReplayEntry(
            seq=event.seq,
            wall=event.stamp.wall_str(),
            elapsed=event.stamp.elapsed,
            text=event.text,
        )
        with self._lock:
            self._entries.append(entry)

    # ── reading it back ────────────────────────────────────────────────────

    def snapshot(self, limit: Optional[int] = None) -> list[ReplayEntry]:
        """The most recent ``limit`` entries, oldest first.

        A copy under the lock: a client can be slow to send without holding up
        the read thread that is still appending.
        """
        with self._lock:
            entries = list(self._entries)
        if limit is not None and limit >= 0:
            entries = entries[len(entries) - limit:] if limit else []
        return entries

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    @property
    def enabled(self) -> bool:
        return self._enabled


def describe(entries: list[ReplayEntry]) -> str:
    """A one-line summary for the divider a UI shows above replayed output."""
    if not entries:
        return "no history"
    span = entries[-1].elapsed - entries[0].elapsed
    if span >= 3600:
        length = f"{int(span // 3600)}h{int((span % 3600) // 60):02d}m"
    elif span >= 60:
        length = f"{int(span // 60)}m{int(span % 60):02d}s"
    else:
        length = f"{span:.1f}s"
    return (f"{len(entries)} line{'s' if len(entries) != 1 else ''} · "
            f"{entries[0].wall} → {entries[-1].wall} ({length})")
