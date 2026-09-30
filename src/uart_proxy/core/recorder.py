"""
Multi-stream file recorder.

Subscribes to the event bus and writes up to three files for a session:

* ``<base>.log`` — raw RX bytes exactly as received (the pure device log).
* ``<base>-timestamp.log`` — one RX line per row, prefixed with the relative
  elapsed time: ``[00:00:10.0000] line``.
* ``<base>-fulltimestamp.log`` — one RX line per row, prefixed with both the
  local wall-clock time and the relative elapsed time:
  ``[2026-06-12 08:40:20 | 00:00:10.0000] line``.

TX lines (what the operator typed) can optionally be mirrored into the
timestamped files with a ``>>`` marker via ``include_tx``.

The two timestamped files open and close with ``#`` comment lines saying what
was recorded — version, port and settings, and the time window (SPEC S25). The
raw ``.log`` never gets one: it is the device's bytes and nothing else.

A long session can be split into parts (``rotate_bytes``, SPEC S32): once any
file passes the limit, all three are closed together and renamed
``<base>.001.log``, ``<base>-timestamp.001.log``, … and fresh ones opened, so a
part's three files always cover the same span. ``keep_parts`` deletes the
oldest parts beyond that many — which is what actually caps the size of a run
that never ends; retention (S11) only prunes whole *finished* sessions.
"""

from __future__ import annotations

import logging
import os
from typing import TextIO

from .events import Direction, Event, EventKind

logger = logging.getLogger(__name__)


class Recorder:
    def __init__(
        self,
        output_dir: str,
        base_name: str = "output",
        *,
        raw: bool = True,
        relative: bool = True,
        full: bool = True,
        include_tx: bool = False,
        append: bool = False,
        rotate_bytes: int = 0,
        keep_parts: int = 0,
    ) -> None:
        os.makedirs(output_dir, exist_ok=True)
        self._include_tx = include_tx
        self._base = os.path.join(output_dir, base_name)
        self._wanted = {"raw": raw, "relative": relative, "full": full}
        self.rotate_bytes = rotate_bytes
        self.keep_parts = keep_parts
        #: Finished parts, oldest first: each a list of the paths it holds.
        self.parts: list[list[str]] = []
        #: Re-marked at the top of every new part, so each reads on its own.
        self.banner: list[str] = []
        # Bytes in the current part: the raw file, and the larger text file.
        self._raw_bytes = 0
        self._text_bytes = 0

        self._raw_f: TextIO | None = None  # opened in binary; typed loosely
        self._rel_f: TextIO | None = None
        self._full_f: TextIO | None = None

        self.raw_path = f"{self._base}.log"
        self.relative_path = f"{self._base}-timestamp.log"
        self.full_path = f"{self._base}-fulltimestamp.log"
        self._open_files(append=append)

    def _open_files(self, *, append: bool) -> None:
        text_mode = "a" if append else "w"
        bin_mode = "ab" if append else "wb"
        if self._wanted["raw"]:
            self._raw_f = open(self.raw_path, bin_mode)  # noqa: SIM115
        if self._wanted["relative"]:
            self._rel_f = open(self.relative_path, text_mode, encoding="utf-8")  # noqa: SIM115
        if self._wanted["full"]:
            self._full_f = open(self.full_path, text_mode, encoding="utf-8")  # noqa: SIM115

    def handle(self, event: Event) -> None:
        """Bus subscriber entry point."""
        if event.direction == Direction.RX:
            if event.kind == EventKind.DATA and self._raw_f is not None:
                self._raw_f.write(event.data)
                self._raw_f.flush()
                self._count(len(event.data))
            elif event.kind == EventKind.LINE:
                self._write_line(event, marker="")
        elif event.direction == Direction.TX and self._include_tx:
            if event.kind == EventKind.LINE:
                self._write_line(event, marker=">> ")

    def set_banner(self, lines: list[str]) -> None:
        """Mark ``lines`` now, and again at the top of every later part."""
        self.banner = list(lines)
        for line in self.banner:
            self.mark(line)

    def _count(self, size: int) -> None:
        self._raw_bytes += size
        self._maybe_rotate()

    def _count_text(self, size: int) -> None:
        self._text_bytes += size
        self._maybe_rotate(at_line_end=True)

    def _maybe_rotate(self, *, at_line_end: bool = False) -> None:
        """Rotate once over the limit — at the end of a line, so a part's text
        files hold whole lines. A stream that never sends a newline (binary)
        would never reach one, so past twice the limit it rotates anyway."""
        if self.rotate_bytes <= 0:
            return
        over = max(self._raw_bytes, self._text_bytes) >= self.rotate_bytes
        no_lines = self._rel_f is None and self._full_f is None
        if (over and (at_line_end or no_lines)) or self._raw_bytes >= 2 * self.rotate_bytes:
            self.rotate()

    def rotate(self) -> None:
        """Finish the current part and start a fresh one (see module doc)."""
        number = self._next_part_number()
        self.mark(f"continues in part {number + 1}")
        current = self.paths
        self._close_files()
        finished = []
        for path in current:
            stem, ext = os.path.splitext(path)
            target = f"{stem}.{number:03d}{ext}"
            os.replace(path, target)
            finished.append(target)
        self.parts.append(finished)
        self._open_files(append=False)
        self._raw_bytes = self._text_bytes = 0
        for line in self.banner:
            self.mark(line)
        self.mark(f"part {number + 1} — part {number} is "
                  f"{os.path.basename(finished[0]) if finished else '?'}")
        self._prune_parts()

    def _next_part_number(self) -> int:
        """One past the highest part on disk — never onto an earlier run's part
        (``--log-append`` reuses the folder)."""
        import re

        directory, base = os.path.split(self._base)
        pattern = re.compile(rf"^{re.escape(base)}(?:-timestamp|-fulltimestamp)?"
                             rf"\.(\d{{3,}})\.log$")
        highest = 0
        try:
            for name in os.listdir(directory or "."):
                match = pattern.match(name)
                if match:
                    highest = max(highest, int(match.group(1)))
        except OSError:
            pass
        return highest + 1

    def _prune_parts(self) -> None:
        if self.keep_parts <= 0:
            return
        while len(self.parts) > self.keep_parts:
            for path in self.parts.pop(0):
                try:
                    os.unlink(path)
                except OSError:
                    logger.warning("could not remove old part %s", path, exc_info=True)

    def mark(self, text: str) -> None:
        """Write ``# text`` into the timestamped files (never the raw log).

        ``#`` because a device line is always prefixed ``[stamp]``, so a
        comment line can never be mistaken for one.
        """
        for f in (self._rel_f, self._full_f):
            if f is not None:
                f.write(f"# {text}\n")
                f.flush()

    def _write_line(self, event: Event, marker: str) -> None:
        if self._rel_f is not None:
            rel_row = f"[{event.stamp.elapsed_str()}] {marker}{event.text}\n"
            self._rel_f.write(rel_row)
            self._rel_f.flush()
            if self._full_f is None:
                self._count_text(len(rel_row.encode("utf-8")))
        if self._full_f is not None:
            row = (f"[{event.stamp.wall_str()} | {event.stamp.elapsed_str()}] "
                   f"{marker}{event.text}\n")
            self._full_f.write(row)
            self._full_f.flush()
            # The full-stamp file grows fastest (≈35 B of stamp per line), so
            # it is the one that decides when the text files reach the limit.
            self._count_text(len(row.encode("utf-8")))

    def close(self) -> None:
        self._close_files()

    def _close_files(self) -> None:
        for f in (self._raw_f, self._rel_f, self._full_f):
            if f is not None:
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    logger.warning("Error closing recorder file", exc_info=True)
        self._raw_f = self._rel_f = self._full_f = None

    @property
    def paths(self) -> list[str]:
        out = []
        if self._raw_f is not None:
            out.append(self.raw_path)
        if self._rel_f is not None:
            out.append(self.relative_path)
        if self._full_f is not None:
            out.append(self.full_path)
        return out
