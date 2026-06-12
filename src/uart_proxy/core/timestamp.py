"""
Dual time-axis tracking.

Every session keeps two views of time:

* **Absolute wall clock** — the local date/time of an event, e.g.
  ``2026-06-12 08:40:10``. A window reads
  ``2026-06-12 08:40:10 ~ 2026-06-12 08:40:20 (10s)``.
* **Relative elapsed time** — time since the session started, formatted as
  ``HH:MM:SS.ffff``. A window reads ``00:00:00.0000 ~ 00:00:10.0000``.

Wall-clock event times are derived from a monotonic clock plus the captured
start time, so they never jump backwards if the system clock is adjusted
mid-session.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta

WALL_FORMAT = "%Y-%m-%d %H:%M:%S"


def format_elapsed(seconds: float) -> str:
    """Format a duration in seconds as ``HH:MM:SS.ffff`` (4 decimal places)."""
    if seconds < 0:
        seconds = 0.0
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    # ``07.4f`` => two integer digits, a dot, four decimals (e.g. "10.0000").
    return f"{hours:02d}:{minutes:02d}:{secs:07.4f}"


@dataclass(frozen=True)
class Stamp:
    """A single point in time, expressed in both axes."""

    wall: datetime  # local wall-clock time of the event
    elapsed: float  # seconds since the session started

    def wall_str(self, fmt: str = WALL_FORMAT) -> str:
        return self.wall.strftime(fmt)

    def elapsed_str(self) -> str:
        return format_elapsed(self.elapsed)


class TimestampTracker:
    """Produces :class:`Stamp` values relative to a fixed session start."""

    def __init__(self) -> None:
        self._start_wall = datetime.now()
        self._start_mono = time.monotonic()

    @property
    def start_wall(self) -> datetime:
        return self._start_wall

    def stamp(self) -> Stamp:
        """Capture the current instant in both axes."""
        elapsed = time.monotonic() - self._start_mono
        wall = self._start_wall + timedelta(seconds=elapsed)
        return Stamp(wall=wall, elapsed=elapsed)

    def abs_window(self, end: Stamp | None = None) -> str:
        """``2026-06-12 08:40:10 ~ 2026-06-12 08:40:20 (10s)``."""
        end = end or self.stamp()
        return (
            f"{self._start_wall.strftime(WALL_FORMAT)} ~ "
            f"{end.wall.strftime(WALL_FORMAT)} ({end.elapsed:.0f}s)"
        )

    def rel_window(self, end: Stamp | None = None) -> str:
        """``00:00:00.0000 ~ 00:00:10.0000``."""
        end = end or self.stamp()
        return f"{format_elapsed(0.0)} ~ {format_elapsed(end.elapsed)}"
