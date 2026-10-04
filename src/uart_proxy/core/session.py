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
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from ..io.source import SourceRefused
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
        # S43: the device's health — its last published state, since when,
        # with what error; how often it came back; when it last spoke.
        self._health_lock = threading.Lock()
        self._device_state = "connecting"
        self._device_since = datetime.now()
        self._device_error: Optional[str] = None
        self._reconnects = 0
        self._ever_connected = False
        self._last_output: Optional[datetime] = None

        self._rx_asm = LineAssembler()
        # What is typed ends a line on Enter, and Enter is often a bare \r.
        self._tx_asm = LineAssembler(cr_ends_line=True)
        self._seq = itertools.count(1)

        self._conn_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._running = False     # session is active (start..stop)
        self._connected = False   # source is currently open
        # Guards the running -> stopped transition, which either stop() or the
        # manager giving up on its own can make, and which must be announced
        # exactly once.
        self._end_lock = threading.Lock()

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
        if not self._end():
            return
        self._stop.set()
        if self._conn_thread is not None:
            self._conn_thread.join(timeout=3.0)
        self._publish_status("disconnected", {})

    def _end(self) -> bool:
        """Mark the session stopped; True for whichever caller got there first."""
        with self._end_lock:
            if not self._running:
                return False
            self._running = False
            return True

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── connection manager ───────────────────────────────────────────────────

    def _run_manager(self) -> None:
        """Open → read → (on drop) reconnect, until stop()."""
        # A missing device is retried every reconnect_interval, possibly for
        # hours; one `waiting` line per attempt would bury everything else, so
        # it is repeated only when the reason changes (absent -> busy, say).
        last_wait: Optional[str] = None
        refused = False
        while not self._stop.is_set():
            try:
                self.source.open()
            except SourceRefused as exc:
                # Refused, not absent: the same answer every time, and against a
                # rate-limited proxy a retry loop gets our own address banned.
                self._publish_status(
                    "error",
                    {"source": self.source.description(), "error": str(exc),
                     "refused": True},
                )
                refused = True
                break
            except Exception as exc:  # noqa: BLE001 - any other open failure is retryable
                if str(exc) != last_wait:
                    last_wait = str(exc)
                    self._publish_status(
                        "waiting",
                        {"source": self.source.description(), "error": last_wait},
                    )
                if not self.auto_reconnect:
                    break
                self._stop.wait(self.reconnect_interval)
                continue

            last_wait = None
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
        # Gave up on its own (no auto-reconnect): say so, or a UI waiting for
        # the session to end — headless mode — would wait forever.
        if self._end():
            self._publish_status("disconnected",
                                 {"reason": "refused" if refused else "gave up"})

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
        self._last_output = datetime.now()
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

    def write(self, data: bytes, *, origin: Optional[dict] = None) -> int:
        """Send raw bytes to the source and publish TX events.

        ``origin`` says who sent them (SPEC S40) — the proxy passes its client —
        and travels in the TX events' ``meta["origin"]``. A line completed by
        this write carries this write's origin.
        """
        meta = {"origin": origin} if origin is not None else {}
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
                meta=dict(meta),
            )
        )
        for raw_line in self._tx_asm.feed(data):
            self._emit_line(raw_line, Direction.TX, meta)
        return written

    def send_text(self, text: str, eol: Optional[bytes] = None) -> int:
        """Encode ``text``, append the line ending, and send it."""
        suffix = self.default_eol if eol is None else eol
        return self.write(text.encode(self.encoding, errors="replace") + suffix)

    # ── helpers ──────────────────────────────────────────────────────────────

    def _emit_line(self, raw_line: bytes, direction: Direction,
                   meta: Optional[dict] = None) -> None:
        stamp = self.tracker.stamp()
        self._emit(
            Event(
                kind=EventKind.LINE,
                direction=direction,
                stamp=stamp,
                seq=next(self._seq),
                data=raw_line,
                text=self._decode(raw_line),
                meta=dict(meta or {}),
            )
        )

    def _flush_pending(self, asm: LineAssembler, direction: Direction) -> None:
        raw = asm.flush()
        if raw is not None:
            self._emit_line(raw, direction)

    def device_health(self) -> dict:
        """S43: the device's state, since when, its error, reconnects, last output."""
        with self._health_lock:
            return {"state": self._device_state,
                    "since": self._device_since.strftime("%Y-%m-%d %H:%M:%S"),
                    "since_epoch": self._device_since.timestamp(),
                    "error": self._device_error,
                    "reconnects": self._reconnects,
                    "last_output": (self._last_output.strftime("%Y-%m-%d %H:%M:%S")
                                    if self._last_output else None),
                    "last_output_epoch": self._last_output.timestamp() if self._last_output else None}

    def _publish_status(self, state: str, meta: dict) -> None:
        with self._health_lock:
            if state == "connected":
                if self._ever_connected:
                    self._reconnects += 1
                self._ever_connected = True
                self._device_error = None
            elif "error" in meta:
                self._device_error = str(meta["error"])
            if state != self._device_state or state == "connected":
                self._device_since = datetime.now()
            self._device_state = state
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

    def publish_remote_tx(self, text: str) -> None:
        """Show a line someone else typed into the device (SPEC S29).

        A TX line like our own, so the log and ``--log-tx`` treat it the same —
        but nothing is written: it already reached the device, through the
        proxy we are a client of.
        """
        self._emit(
            Event(
                kind=EventKind.LINE,
                direction=Direction.TX,
                stamp=self.tracker.stamp(),
                seq=next(self._seq),
                text=text,
                meta={"remote": True},
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
