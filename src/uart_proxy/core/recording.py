"""
A recorded session, loaded for playback (SPEC S37).

``output.log`` holds the device's bytes and ``output-timing.log`` beside it says
when each run of them arrived: ``<epoch> <elapsed> <bytes>`` per row, or — in
the first version of the format — ``<elapsed> <bytes>``. Together they are
enough to play a session back at its own pace — through a terminal emulator,
so a full-screen program (a BBS, ``vi``, a boot menu) reads as it looked, which
a log of its cursor-movement codes never will.

Loading is forgiving: without a timing file the bytes are there and simply have
no pace; rows that claim more bytes than the log holds (a recording cut off by
a crash) are trimmed to what exists.
"""

from __future__ import annotations

import bisect
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

_PART = re.compile(r"^(?P<base>.+?)(?P<part>\.\d{3,})?\.log$")
_STARTED = re.compile(r"started (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


@dataclass
class Recording:
    raw_path: str
    data: bytes
    #: The playback timeline, one entry per chunk, ascending: UTC epoch seconds
    #: when the timing file has them, else session elapsed.
    times: list[float] = field(default_factory=list)
    #: Session elapsed of each chunk (what the timestamped logs show).
    elapsed: list[float] = field(default_factory=list)
    #: Whether ``times`` are epoch seconds (three-column timing).
    epoch_based: bool = False
    #: Offset into ``data`` where each chunk ends (cumulative).
    ends: list[int] = field(default_factory=list)
    #: Wall-clock time of elapsed 0, from the log banner (SPEC S25), if known.
    start_wall: Optional[datetime] = None
    has_timing: bool = False

    @property
    def origin(self) -> float:
        """Elapsed time of the first chunk: playback position 0."""
        return self.times[0] if self.times else 0.0

    @property
    def duration(self) -> float:
        return (self.times[-1] - self.origin) if self.times else 0.0

    def offset_at(self, position: float) -> int:
        """How many bytes had arrived ``position`` seconds into playback."""
        if not self.times:
            return len(self.data)
        index = bisect.bisect_right(self.times, self.origin + position)
        return self.ends[index - 1] if index else 0

    def next_time_after(self, position: float) -> Optional[float]:
        """Playback position of the first chunk later than ``position``."""
        index = bisect.bisect_right(self.times, self.origin + position)
        return self.times[index] - self.origin if index < len(self.times) else None

    def previous_time_at(self, position: float) -> float:
        """Playback position of the last chunk at or before ``position``."""
        index = bisect.bisect_right(self.times, self.origin + position)
        return self.times[index - 1] - self.origin if index else 0.0

    def wall_at(self, position: float) -> Optional[datetime]:
        if self.epoch_based:
            return datetime.fromtimestamp(self.origin + position)
        if self.start_wall is None:
            return None
        return self.start_wall + timedelta(seconds=self.origin + position)

    def position_of_wall(self, when: datetime) -> Optional[float]:
        """Playback position of a wall-clock moment, if the recording knows walls."""
        if self.epoch_based:
            return when.timestamp() - self.origin
        if self.start_wall is None:
            return None
        return (when - self.start_wall).total_seconds() - self.origin

    def position_of_elapsed(self, elapsed: float) -> float:
        """Playback position where the session's elapsed first reached ``elapsed``
        — the first run's, if runs were appended to one folder."""
        for time_, run_elapsed in zip(self.times, self.elapsed):
            if run_elapsed >= elapsed:
                # Interpolate back to the exact moment within this chunk's gap.
                return max(0.0, time_ - self.origin - (run_elapsed - elapsed))
        return self.duration


_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}(?:\.\d+)?))?$")


def _seconds(text: str) -> float:
    """``90``, ``1:30``, ``00:01:30.5`` → seconds."""
    parts = text.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError(text)
    value = 0.0
    for part in parts:
        value = value * 60 + float(part)
    return value


def parse_at(text: str, recording: Recording) -> float:
    """Where ``--at`` / ``g`` means, as a playback position. Raises ValueError.

    * ``+00:12:40`` or ``+760`` — session elapsed, as the timestamped logs show;
    * ``03:12:40`` — that time of day, on the first day of the recording that
      has it (or the next, for a recording that runs past midnight);
    * ``2026-09-30 03:12:40`` — that exact moment.
    """
    text = text.strip()
    if text.startswith("+"):
        return recording.position_of_elapsed(_seconds(text[1:]))
    try:
        when = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        when = None
    if when is None:
        match = _CLOCK.match(text)
        if not match:
            raise ValueError(f"not a time: {text!r} (use +HH:MM:SS, HH:MM:SS or "
                             f"'YYYY-mm-dd HH:MM:SS')")
        first = recording.wall_at(0.0)
        if first is None:
            raise ValueError("this recording has no wall-clock times; use +HH:MM:SS")
        hour, minute = int(match.group(1)), int(match.group(2))
        second = float(match.group(3) or 0)
        when = first.replace(hour=hour, minute=minute, second=int(second),
                             microsecond=int((second % 1) * 1e6))
        if when < first:
            when += timedelta(days=1)
    position = recording.position_of_wall(when)
    if position is None:
        raise ValueError("this recording has no wall-clock times; use +HH:MM:SS")
    return position


def sibling(raw_path: str, suffix: str) -> str:
    """``output.log`` → ``output-<suffix>.log``; ``output.003.log`` →
    ``output-<suffix>.003.log`` — the same part of the same recording."""
    directory, name = os.path.split(raw_path)
    match = _PART.match(name)
    if not match:
        return os.path.join(directory, f"{name}-{suffix}")
    part = match.group("part") or ""
    return os.path.join(directory, f"{match.group('base')}-{suffix}{part}.log")


def resolve(path: Optional[str], sessions_root: str) -> str:
    """The raw log to play: a file as given, ``output.log`` in a directory, or
    — with no path — the newest session in the managed store."""
    if path is None:
        try:
            folders = sorted(
                (os.path.join(sessions_root, d) for d in os.listdir(sessions_root)),
                key=os.path.getmtime)
        except OSError:
            folders = []
        folders = [f for f in folders if os.path.isfile(os.path.join(f, "output.log"))]
        if not folders:
            raise FileNotFoundError(f"no recorded sessions in {sessions_root}")
        return os.path.join(folders[-1], "output.log")
    if os.path.isdir(path):
        candidate = os.path.join(path, "output.log")
        if not os.path.isfile(candidate):
            raise FileNotFoundError(f"no output.log in {path}")
        return candidate
    if not os.path.isfile(path):
        raise FileNotFoundError(f"{path} does not exist")
    return path


def load(raw_path: str) -> Recording:
    with open(raw_path, "rb") as fh:
        data = fh.read()
    recording = Recording(raw_path=raw_path, data=data)

    timing_path = sibling(raw_path, "timing")
    if os.path.isfile(timing_path):
        total = 0
        columns = None      # decided by the first good row: 3 (epoch) or 2
        with open(timing_path, encoding="utf-8", errors="replace") as fh:
            for row in fh:
                fields = row.split()
                if row.startswith("#") or len(fields) not in (2, 3):
                    continue
                if columns is None:
                    columns = len(fields)
                if len(fields) != columns:
                    continue
                try:
                    if columns == 3:
                        stamp, run_elapsed, size = (float(fields[0]), float(fields[1]),
                                                    int(fields[2]))
                    else:
                        stamp, size = float(fields[0]), int(fields[1])
                        run_elapsed = stamp
                except ValueError:
                    continue
                if size <= 0 or total >= len(data):
                    continue
                total = min(total + size, len(data))
                if recording.times and stamp < recording.times[-1]:
                    # Never backwards. With epoch times that only happens if the
                    # clock was set back between two appended runs; with the old
                    # elapsed-only format it is what a second appended run looks
                    # like, which that format cannot place — so it is squashed.
                    stamp = recording.times[-1]
                recording.times.append(stamp)
                recording.elapsed.append(run_elapsed)
                recording.ends.append(total)
        recording.epoch_based = columns == 3
        if recording.times and total < len(data):
            # Bytes the timing never mentioned (it was cut short): at the end.
            recording.times.append(recording.times[-1])
            recording.elapsed.append(recording.elapsed[-1])
            recording.ends.append(len(data))
        recording.has_timing = bool(recording.times)

    banner = sibling(raw_path, "timestamp")
    if os.path.isfile(banner):
        with open(banner, encoding="utf-8", errors="replace") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line.startswith("#"):
                    break
                match = _STARTED.search(line)
                if match:
                    recording.start_wall = datetime.strptime(match.group(1),
                                                             "%Y-%m-%d %H:%M:%S")
                    break
    return recording
