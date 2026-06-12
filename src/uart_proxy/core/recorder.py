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
    ) -> None:
        os.makedirs(output_dir, exist_ok=True)
        self._include_tx = include_tx

        text_mode = "a" if append else "w"
        bin_mode = "ab" if append else "wb"
        base = os.path.join(output_dir, base_name)

        self._raw_f: TextIO | None = None  # opened in binary; typed loosely
        self._rel_f: TextIO | None = None
        self._full_f: TextIO | None = None

        self.raw_path = f"{base}.log"
        self.relative_path = f"{base}-timestamp.log"
        self.full_path = f"{base}-fulltimestamp.log"

        if raw:
            self._raw_f = open(self.raw_path, bin_mode)  # noqa: SIM115
        if relative:
            self._rel_f = open(self.relative_path, text_mode, encoding="utf-8")  # noqa: SIM115
        if full:
            self._full_f = open(self.full_path, text_mode, encoding="utf-8")  # noqa: SIM115

    def handle(self, event: Event) -> None:
        """Bus subscriber entry point."""
        if event.direction == Direction.RX:
            if event.kind == EventKind.DATA and self._raw_f is not None:
                self._raw_f.write(event.data)
                self._raw_f.flush()
            elif event.kind == EventKind.LINE:
                self._write_line(event, marker="")
        elif event.direction == Direction.TX and self._include_tx:
            if event.kind == EventKind.LINE:
                self._write_line(event, marker=">> ")

    def _write_line(self, event: Event, marker: str) -> None:
        if self._rel_f is not None:
            self._rel_f.write(f"[{event.stamp.elapsed_str()}] {marker}{event.text}\n")
            self._rel_f.flush()
        if self._full_f is not None:
            self._full_f.write(
                f"[{event.stamp.wall_str()} | {event.stamp.elapsed_str()}] "
                f"{marker}{event.text}\n"
            )
            self._full_f.flush()

    def close(self) -> None:
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
