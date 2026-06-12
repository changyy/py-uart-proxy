"""
The session data pump.

``UartSession`` is the heart of the app and is deliberately unaware of *what*
the underlying transport is — it drives a :class:`~uart_proxy.io.source.DataSource`
(local UART or remote socket) and turns the raw byte traffic into a stream of
:class:`~uart_proxy.core.events.Event` objects on the bus.

Pipeline per received chunk:

    bytes ──► DATA event (RX)        → live display + raw log + proxy fan-out
          └─► LineAssembler ──► LINE event (RX)  → timestamped logs + plugins

A short idle flush emits buffered partial lines (e.g. ``login: ``) so prompts
that lack a trailing newline still appear.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from typing import TYPE_CHECKING, Optional

from .bus import EventBus
from .events import Direction, Event, EventKind
from .line_assembler import LineAssembler
from .timestamp import TimestampTracker

if TYPE_CHECKING:  # avoid a circular import; only needed for type hints
    from ..io.source import DataSource

logger = logging.getLogger(__name__)

_READ_CHUNK = 4096
_READ_TIMEOUT = 0.1   # seconds per read attempt
_IDLE_FLUSH = 0.2     # flush a partial RX line after this much silence


class UartSession:
    def __init__(
        self,
        source: "DataSource",
        *,
        bus: Optional[EventBus] = None,
        tracker: Optional[TimestampTracker] = None,
        encoding: str = "utf-8",
        default_eol: bytes = b"\r\n",
        auto_reconnect: bool = True,
        reconnect_interval: float = 1.0,
    ) -> None:
        self.source = source
        self.bus = bus or EventBus()
        self.tracker = tracker or TimestampTracker()
        self.encoding = encoding
        self.default_eol = default_eol
        self.auto_reconnect = auto_reconnect
        self.reconnect_interval = reconnect_interval

        self._rx_asm = LineAssembler()
        self._tx_asm = LineAssembler()
        self._seq = itertools.count(1)

        self._conn_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running = False     # session is active (start..stop)
        self._connected = False   # source is currently open

        self.rx_bytes = 0
        self.tx_bytes = 0

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Begin the session on a background thread.

        Returns immediately. The connection manager opens the source, and — when
        ``auto_reconnect`` is set — keeps retrying if the device is missing at
        start or disappears mid-session, re-attaching automatically when it
        comes back. Connection state is reported via STATUS events.
        """
        self._running = True
        self._stop.clear()
        self._conn_thread = threading.Thread(
            target=self._run_manager, name="uart-conn", daemon=True
        )
        self._conn_thread.start()

    def stop(self) -> None:
        """Stop the session, flush any partial line, and close the source."""
        if not self._running:
            return
        self._running = False
        self._stop.set()
        if self._conn_thread is not None:
            self._conn_thread.join(timeout=3.0)
        self._publish_status("disconnected", {})

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── connection manager ───────────────────────────────────────────────────

    def _run_manager(self) -> None:
        """Open → read → (on drop) reconnect, until stop()."""
        while not self._stop.is_set():
            try:
                self.source.open()
            except Exception as exc:  # noqa: BLE001 - any open failure is retryable
                self._publish_status(
                    "waiting",
                    {"source": self.source.description(), "error": str(exc)},
                )
                if not self.auto_reconnect:
                    break
                self._stop.wait(self.reconnect_interval)
                continue

            self._connected = True
            self._publish_status("connected", {"source": self.source.description()})

            self._read_loop()  # returns on stop() or a read error

            self._connected = False
            self._flush_pending(self._rx_asm, Direction.RX)
            try:
                self.source.close()
            except Exception:  # noqa: BLE001
                logger.warning("Error closing source", exc_info=True)

            if self._stop.is_set() or not self.auto_reconnect:
                break
            self._publish_status("reconnecting", {"source": self.source.description()})
            self._stop.wait(self.reconnect_interval)

        self._connected = False
        self._running = False

    # ── read path ──────────────────────────────────────────────────────────

    def _read_loop(self) -> None:
        last_data = time.monotonic()
        while not self._stop.is_set():
            try:
                data = self.source.read(_READ_CHUNK, timeout=_READ_TIMEOUT)
            except Exception as exc:  # noqa: BLE001 - device drop / transport error
                self._publish_status("error", {"error": str(exc)})
                return
            if data:
                self._on_rx(data)
                last_data = time.monotonic()
            elif self._rx_asm.has_pending and (time.monotonic() - last_data) > _IDLE_FLUSH:
                self._flush_pending(self._rx_asm, Direction.RX)
                last_data = time.monotonic()

    def _on_rx(self, data: bytes) -> None:
        self.rx_bytes += len(data)
        stamp = self.tracker.stamp()
        self._emit(
            Event(
                kind=EventKind.DATA,
                direction=Direction.RX,
                stamp=stamp,
                seq=next(self._seq),
                data=data,
                text=self._decode(data),
            )
        )
        for raw_line in self._rx_asm.feed(data):
            self._emit_line(raw_line, Direction.RX)

    # ── write path ───────────────────────────────────────────────────────────

    def write(self, data: bytes) -> int:
        """Send raw bytes to the source and publish TX events."""
        if not self._connected:
            raise RuntimeError("not connected (waiting for the device)")
        written = self.source.write(data)
        self.tx_bytes += written
        stamp = self.tracker.stamp()
        self._emit(
            Event(
                kind=EventKind.DATA,
                direction=Direction.TX,
                stamp=stamp,
                seq=next(self._seq),
                data=data,
                text=self._decode(data),
            )
        )
        for raw_line in self._tx_asm.feed(data):
            self._emit_line(raw_line, Direction.TX)
        return written

    def send_text(self, text: str, eol: Optional[bytes] = None) -> int:
        """Encode ``text``, append the line ending, and send it."""
        suffix = self.default_eol if eol is None else eol
        return self.write(text.encode(self.encoding, errors="replace") + suffix)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _emit_line(self, raw_line: bytes, direction: Direction) -> None:
        stamp = self.tracker.stamp()
        self._emit(
            Event(
                kind=EventKind.LINE,
                direction=direction,
                stamp=stamp,
                seq=next(self._seq),
                data=raw_line,
                text=self._decode(raw_line),
            )
        )

    def _flush_pending(self, asm: LineAssembler, direction: Direction) -> None:
        raw = asm.flush()
        if raw is not None:
            self._emit_line(raw, direction)

    def _publish_status(self, state: str, meta: dict) -> None:
        self._emit(
            Event(
                kind=EventKind.STATUS,
                direction=Direction.SYS,
                stamp=self.tracker.stamp(),
                seq=next(self._seq),
                text=state,
                meta=meta,
            )
        )

    def publish_notice(self, text: str, meta: Optional[dict] = None) -> None:
        """Used by plugins to surface a message into the stream."""
        self._emit(
            Event(
                kind=EventKind.NOTICE,
                direction=Direction.SYS,
                stamp=self.tracker.stamp(),
                seq=next(self._seq),
                text=text,
                meta=meta or {},
            )
        )

    def _decode(self, data: bytes) -> str:
        return data.decode(self.encoding, errors="replace")

    def _emit(self, event: Event) -> None:
        self.bus.publish(event)
