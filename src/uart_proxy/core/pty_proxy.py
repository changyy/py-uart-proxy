"""
Local PTY mirroring — share one seized UART with several local tools.

``uart-proxy connect`` owns the physical port exclusively (see
:func:`uart_proxy.io.uart_source.seize_exclusive`). That is the right thing for
data integrity, but it also means no other program on this machine can talk to
the device. :class:`PtyProxyGroup` gives those programs a way in: it exposes N
**full-duplex PTY mirrors**, each symlinked into a directory, so any tool that
knows how to open a serial port (``screen``, ``minicom``, pyserial, an agent)
attaches to a mirror as if it were the real device.

    real /dev/cu.usbserial-110
        │   opened once, exclusively, by UartSource
        ▼
    ┌──────────────── PtyProxyGroup ────────────────┐
    │  RX bytes  ──broadcast──►  usbserial-110-0    │  e.g. an AI agent
    │                            usbserial-110-1    │  e.g. a human in screen
    │  TX bytes  ◄──merged onto the wire──  (any mirror may write)
    └───────────────────────────────────────────────┘

A mirror is **raw by default**, because that is what a serial port is and what
every tool attached to one assumes: bytes cross the moment they are typed, so
``^C`` interrupts now rather than eventually. ``--tx-merge line`` trades that
away for whole-command atomicity between competing writers; see ``tx_merge``.

This is an :class:`~uart_proxy.core.bus.EventBus` **sink**, parallel to the
recorder and the socket proxy — it never opens the device itself. RX arrives as
``DATA(RX)`` events via :meth:`PtyProxyGroup.handle`; TX collected from the
mirrors is handed to an ``on_tx`` callable (in practice ``session.write``), so
mirror traffic shows up in the TUI and the logs like any other TX.

Two things it deliberately does *not* do:

* **RX only, no TX echo.** A mirror sees what the device sent, not what another
  mirror typed. Real consoles echo, so the human still sees the agent's command
  — and staying transparent is what lets an unmodified ``screen`` work.
* **No request/response routing.** A UART is one byte stream with no framing, so
  "which reply belongs to which writer" is not answerable at this layer.
  ``--tx-merge line`` keeps whole commands intact; correlating replies is the
  caller's problem.

POSIX only — ``pty`` has no Windows equivalent. Use ``--serve`` (the TCP proxy)
there instead.
"""

from __future__ import annotations

import errno
import logging
import os
import selectors
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

from .events import Direction, Event, EventKind

logger = logging.getLogger(__name__)

#: Whether this platform can create PTY mirrors at all.
PTY_SUPPORTED = sys.platform != "win32" and hasattr(os, "openpty")

if PTY_SUPPORTED:  # pragma: no cover - import guard, exercised on POSIX
    import pty
    import termios
    import tty

DEFAULT_PROXY_DIR = "/tmp/uart-proxy"
DEFAULT_PROXY_COUNT = 2

#: Per-mirror RX backlog cap. A client that stops reading must not be able to
#: stall the device read thread or starve the other mirrors, so past this many
#: pending bytes we drop (and count) instead of growing without bound.
DEFAULT_QUEUE_MAX = 1 << 20  # 1 MiB

#: How stale a mirror's backlog may get before it is thrown away.
#:
#: **A mirror is a live view.** History belongs to the recorder's log files and to
#: `attach`, which can present it with its original timestamps; a raw byte pipe
#: cannot. Without this bound, output that arrived while nobody was attached sits
#: in the queue and is handed to whichever tool opens the mirror next — so a
#: program reads minutes-old output and takes it for the current state, which is
#: worse than missing it. (Most tools happen to flush on open: `screen` sets raw
#: mode with ``TCSAFLUSH``, pyserial calls ``tcflush`` — but that is their
#: accident, not our guarantee.)
#:
#: The window is generous on purpose: a reader that is genuinely attached and
#: merely busy for a moment must not lose bytes, and this cannot tell "nobody is
#: there" apart from "stuck for a while". Losing data from a live reader would be
#: the worse failure of the two.
DEFAULT_MAX_LAG = 5.0  # seconds

#: In line-merge mode, flush a runaway line that never terminates at this size
#: rather than buffering a client's binary upload forever.
DEFAULT_LINE_MAX = 4096

_LINE_TERMS = b"\n\r"  # either byte ends a line for merge purposes

#: Bytes that are asynchronous **signals**, not content. They mean nothing as
#: part of a line and are worse than useless if they arrive late — a ``^C`` that
#: turns up whenever the sender next happens to press Enter interrupts the wrong
#: thing. Line-merge therefore lets them overtake the buffer.
SIGNAL_BYTES = bytes((0x03, 0x04, 0x1A, 0x1C))  # ^C INTR · ^D EOF · ^Z SUSP · ^\ QUIT

#: Anything that ends the chunk currently being held in line-merge mode.
_FLUSH_BYTES = _LINE_TERMS + SIGNAL_BYTES

TX_MERGE_MODES = ("raw", "line")
DEFAULT_TX_MERGE = "raw"

_READ_CHUNK = 65536
_SELECT_TIMEOUT = 0.5  # bounded so stop() is responsive even without a wakeup


def device_stem(device: str) -> str:
    """Mirror name prefix derived from a device path.

    ``/dev/cu.usbserial-110`` and ``/dev/tty.usbserial-110`` both yield
    ``usbserial-110``, so the mirrors are named after the hardware rather than
    which of the two macOS aliases happened to be used.
    """
    base = os.path.basename(device)
    for prefix in ("tty.", "cu."):
        if base.startswith(prefix):
            return base[len(prefix):]
    return base or "uart"


def build_links(
    proxy_dir: str,
    stem: str,
    count: int,
    *,
    extra: Optional[Sequence[str]] = None,
) -> list[str]:
    """Symlink paths for ``count`` auto-named mirrors, plus any explicit ones."""
    links = [os.path.join(proxy_dir, f"{stem}-{i}") for i in range(count)]
    links.extend(extra or [])
    return links


@dataclass
class MirrorStats:
    """A point-in-time snapshot of one mirror, for status display."""

    name: str
    link: str
    slave: str
    rx_bytes: int = 0
    tx_bytes: int = 0
    dropped: int = 0    # discarded because a reader fell behind the size cap
    stale: int = 0      # discarded because nobody drained it in time
    pending: int = 0


class PtyMirror:
    """One full-duplex PTY a client opens through its symlink.

    We hold the *slave* fd open for the lifetime of the mirror but never read
    it: keeping it open stops the master from seeing EIO every time a client
    disconnects, and not reading it means we never steal bytes the client wrote.
    """

    def __init__(self, name: str, link: str, *, queue_max: int = DEFAULT_QUEUE_MAX,
                 max_lag: float = DEFAULT_MAX_LAG) -> None:
        self.name = name
        self.link = link
        self.queue_max = queue_max
        self.max_lag = max_lag

        self.master, self.slave = pty.openpty()
        self.slave_name = os.ttyname(self.slave)
        # Raw on both sides: no echo, no CR/LF translation — a transparent pipe,
        # because the bytes on a UART are already exactly what both ends meant.
        tty.setraw(self.master)
        tty.setraw(self.slave)
        os.set_blocking(self.master, False)

        self.out = bytearray()      # RX pending write to the client
        self.txline = bytearray()   # partial TX line accumulated from the client
        self.rx_bytes = 0
        self.tx_bytes = 0
        self.dropped = 0
        self.stale = 0
        self._progress_at = 0.0     # last time the client took bytes (or a
                                    # backlog started); see drop_if_stale
        self._warned_at = 0         # last `dropped` total we complained about

        _symlink_force(self.slave_name, self.link)

    # ── RX side ────────────────────────────────────────────────────────────

    def enqueue(self, data: bytes) -> None:
        """Queue RX bytes for this client, dropping if it has fallen too far behind."""
        if len(self.out) + len(data) > self.queue_max:
            self.dropped += len(data)
            # One line per 64 KiB, not per chunk: a client that stopped reading
            # would otherwise fill the log faster than the device fills the queue.
            if self.dropped - self._warned_at >= 64 * 1024:
                self._warned_at = self.dropped
                logger.warning("%s: reader is behind, dropped %d B so far",
                               self.name, self.dropped)
            return
        if not self.out:
            self._progress_at = time.monotonic()  # a backlog starts forming now
        self.out += data
        self.rx_bytes += len(data)

    def note_progress(self) -> None:
        """Record that the client took some bytes.

        The staleness rule is "no progress for a while", not "the oldest byte is
        old": a reader that is merely slow but *is* draining must never have its
        backlog thrown away, and only progress tells the two apart.
        """
        self._progress_at = time.monotonic()

    def drop_if_stale(self) -> int:
        """Throw away a backlog nobody is draining, and say how much.

        This is what keeps a mirror a *live* view (see :data:`DEFAULT_MAX_LAG`).
        The kernel's own pty buffer is flushed too, otherwise the couple of KiB it
        absorbed before it filled would still be waiting for the next tool to
        open the mirror.
        """
        if not self.out or self.max_lag <= 0:
            return 0
        if time.monotonic() - self._progress_at < self.max_lag:
            return 0
        discarded = len(self.out)
        self.out.clear()
        self.stale += discarded
        # Also clear what the kernel already absorbed, or the couple of KiB it
        # took before its buffer filled would still greet the next tool to open
        # the mirror. It has to be the *slave's input* queue: that is where bytes
        # written to the master wait. TCOFLUSH on the master does nothing here
        # (measured), and TCIOFLUSH would also throw away the TX a client has
        # just written, which is not ours to drop.
        try:
            termios.tcflush(self.slave, termios.TCIFLUSH)
        except (OSError, termios.error):
            pass
        logger.debug("%s: discarded %d B nobody read within %.1fs",
                     self.name, discarded, self.max_lag)
        return discarded

    # ── teardown ───────────────────────────────────────────────────────────

    def close(self) -> None:
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:  # already closed
                pass
        # Only remove a link that is still ours — never clobber whatever some
        # other process may have put at this path in the meantime.
        try:
            if os.path.islink(self.link) and os.readlink(self.link) == self.slave_name:
                os.unlink(self.link)
        except OSError:
            logger.debug("could not remove %s", self.link, exc_info=True)

    def stats(self) -> MirrorStats:
        return MirrorStats(
            name=self.name,
            link=self.link,
            slave=self.slave_name,
            rx_bytes=self.rx_bytes,
            tx_bytes=self.tx_bytes,
            dropped=self.dropped,
            stale=self.stale,
            pending=len(self.out),
        )


class PtyProxyGroup:
    """N PTY mirrors of one session: RX broadcast out, TX merged back in.

    Parameters
    ----------
    links:
        Symlink paths to create, one per mirror. Also decides the count.
    on_tx:
        Called with the bytes a client wrote, to put them on the wire. Normally
        ``session.write``. Raising is fine and non-fatal (a disconnected session
        raises ``RuntimeError``) — the bytes are dropped and reported.
    tx_merge:
        ``"raw"`` (default) forwards every byte the instant it arrives — what a
        serial port *is*, and what every attached tool assumes: ``^C`` interrupts
        now, tab completion completes, arrow keys reach the shell's history, a
        single-key ``y/n`` prompt works, escape sequences stay intact.

        ``"line"`` instead holds each client's bytes until a line terminator and
        forwards the whole line in one ``on_tx`` call, so two writers typing at
        once cannot splice one command into the middle of another. That costs
        every interactive behaviour listed above, so it is opt-in: take it when
        several *unattended* writers share the wire and a mangled command would
        be worse than a laggy one. :data:`SIGNAL_BYTES` still overtake the buffer
        even here, because a delayed ``^C`` is not a slow ``^C``, it is a wrong one.
    on_notice:
        Optional sink for human-readable operational messages (dropped TX,
        overrun clients). Normally ``session.publish_notice``.
    """

    def __init__(
        self,
        links: Sequence[str],
        on_tx: Callable[[bytes], int],
        *,
        tx_merge: str = DEFAULT_TX_MERGE,
        on_notice: Optional[Callable[[str], None]] = None,
        queue_max: int = DEFAULT_QUEUE_MAX,
        max_lag: float = DEFAULT_MAX_LAG,
        line_max: int = DEFAULT_LINE_MAX,
    ) -> None:
        if tx_merge not in TX_MERGE_MODES:
            raise ValueError(f"tx_merge must be one of {TX_MERGE_MODES}, got {tx_merge!r}")
        if not links:
            raise ValueError("at least one mirror link is required")

        self._links = list(links)
        self._on_tx = on_tx
        self.tx_merge = tx_merge
        self._on_notice = on_notice
        self._queue_max = queue_max
        self._max_lag = max_lag
        self._line_max = line_max

        self.mirrors: list[PtyMirror] = []
        self._lock = threading.Lock()   # guards every mirror buffer
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._wake_r = -1
        self._wake_w = -1
        self._running = False

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        """Create the mirrors and their symlinks, then serve them on a thread."""
        if not PTY_SUPPORTED:
            raise RuntimeError(
                "PTY mirroring needs a POSIX platform (no pty on Windows); "
                "use --serve for a TCP proxy instead"
            )
        if self._running:
            return

        for link in self._links:
            parent = os.path.dirname(os.path.abspath(link))
            os.makedirs(parent, exist_ok=True)

        try:
            for link in self._links:
                mirror = PtyMirror(
                    name=os.path.basename(link), link=link,
                    queue_max=self._queue_max, max_lag=self._max_lag
                )
                self.mirrors.append(mirror)
            self._wake_r, self._wake_w = os.pipe()
            os.set_blocking(self._wake_r, False)
            os.set_blocking(self._wake_w, False)
        except Exception:
            self._teardown()
            raise

        self._stop.clear()
        self._running = True
        self._thread = threading.Thread(target=self._serve, name="pty-proxy", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop serving, close the PTYs, and remove the symlinks."""
        if not self._running:
            return
        self._running = False
        self._stop.set()
        self._wake()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        self._teardown()

    def _teardown(self) -> None:
        for mirror in self.mirrors:
            mirror.close()
        self.mirrors = []
        for fd in (self._wake_r, self._wake_w):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._wake_r = self._wake_w = -1

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def links(self) -> list[str]:
        return list(self._links)

    def stats(self) -> list[MirrorStats]:
        with self._lock:
            return [mirror.stats() for mirror in self.mirrors]

    # ── RX in: bus subscriber ──────────────────────────────────────────────

    def handle(self, event: Event) -> None:
        """EventBus subscriber: broadcast device RX to every mirror.

        Runs on the session's read thread, so it only ever appends to a buffer
        and pokes the serving thread — never blocks on a client.
        """
        if event.kind is EventKind.DATA and event.direction is Direction.RX and event.data:
            self.broadcast(event.data)

    def broadcast(self, data: bytes) -> None:
        with self._lock:
            for mirror in self.mirrors:
                mirror.enqueue(data)
        self._wake()

    # ── the serving loop ───────────────────────────────────────────────────

    def _wake(self) -> None:
        """Nudge the select loop — new RX to flush, or time to stop."""
        if self._wake_w < 0:
            return
        try:
            os.write(self._wake_w, b"\x01")
        except OSError:  # pipe full: a wakeup is already pending, which is enough
            pass

    def _serve(self) -> None:
        with selectors.DefaultSelector() as sel:
            sel.register(self._wake_r, selectors.EVENT_READ, None)
            for mirror in self.mirrors:
                sel.register(mirror.master, selectors.EVENT_READ, mirror)

            while not self._stop.is_set():
                # Ask for writability only where something is actually pending,
                # so an idle mirror doesn't spin the loop. Anything nobody has
                # taken by now is thrown out first: a mirror is a live view, and
                # handing stale output to the next tool that opens it would make
                # a program read the past as the present.
                with self._lock:
                    for mirror in self.mirrors:
                        mirror.drop_if_stale()
                        mask = selectors.EVENT_READ
                        if mirror.out:
                            mask |= selectors.EVENT_WRITE
                        sel.modify(mirror.master, mask, mirror)

                for key, events in sel.select(_SELECT_TIMEOUT):
                    if key.data is None:  # the wakeup pipe
                        _drain(self._wake_r)
                        continue
                    mirror: PtyMirror = key.data
                    if events & selectors.EVENT_READ:
                        self._pump_tx(mirror)
                    if events & selectors.EVENT_WRITE:
                        self._pump_rx(mirror)

    def _pump_rx(self, mirror: PtyMirror) -> None:
        """Write as much queued RX to the client as its PTY will take."""
        with self._lock:
            if not mirror.out:
                return
            chunk = bytes(mirror.out)
        try:
            written = os.write(mirror.master, chunk)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                return  # PTY buffer full; try again next pass
            # Unwritable for any other reason: drop what we have rather than
            # wedge the mirror forever.
            logger.warning("%s: write failed (%s); dropping %d B",
                           mirror.name, exc, len(chunk))
            with self._lock:
                mirror.dropped += len(mirror.out)
                mirror.out.clear()
            return
        with self._lock:
            del mirror.out[:written]
            if written:
                mirror.note_progress()  # it is reading, so it is not stale

    def _pump_tx(self, mirror: PtyMirror) -> None:
        """Read what the client typed and merge it onto the wire."""
        try:
            data = os.read(mirror.master, _READ_CHUNK)
        except OSError as exc:
            if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK):
                logger.debug("%s: read failed", mirror.name, exc_info=True)
            return
        if not data:
            return

        if self.tx_merge == "raw":
            self._forward(mirror, data)
            return

        with self._lock:
            mirror.txline += data
            ready: list[bytes] = []
            while True:
                cut = _first_flush_point(mirror.txline)
                if cut is None:
                    break
                ready.append(bytes(mirror.txline[: cut + 1]))
                del mirror.txline[: cut + 1]
            if len(mirror.txline) >= self._line_max:
                ready.append(bytes(mirror.txline))
                mirror.txline.clear()
        for line in ready:
            self._forward(mirror, line)

    def _forward(self, mirror: PtyMirror, data: bytes) -> None:
        try:
            self._on_tx(data)
        except Exception as exc:  # noqa: BLE001 - a dead device must not kill the loop
            self._notice(f"{mirror.name}: dropped {len(data)} B of TX ({exc})")
            return
        mirror.tx_bytes += len(data)

    def _notice(self, text: str) -> None:
        logger.info("%s", text)
        if self._on_notice is not None:
            try:
                self._on_notice(text)
            except Exception:  # noqa: BLE001
                logger.debug("notice sink raised", exc_info=True)


# ── helpers ────────────────────────────────────────────────────────────────


def _first_flush_point(buf: bytearray) -> Optional[int]:
    """Index of the earliest byte in ``buf`` that ends the held chunk, or None.

    That is a line terminator — the point of line-merge — **or** one of
    :data:`SIGNAL_BYTES`. Both cases emit ``buf[: i + 1]``, so a ``^C`` typed
    after a half-finished command sends that partial text *and* the ``^C``
    together, in one write: the signal is never delayed, and bytes the client
    already handed us are never silently discarded (the device's own line
    discipline is what cancels the abandoned line).
    """
    found = [i for i in (buf.find(bytes([b])) for b in _FLUSH_BYTES) if i >= 0]
    return min(found) if found else None


def _symlink_force(target: str, link: str) -> None:
    """Point ``link`` at ``target``, clearing a stale symlink first.

    A previous run killed with SIGKILL leaves a dangling symlink behind, so
    replacing one is normal. A *non*-symlink at that path is not ours to delete
    — that's a misconfigured ``--proxy`` path, and we say so.
    """
    if os.path.islink(link):
        try:
            os.unlink(link)
        except OSError:
            logger.debug("could not clear stale link %s", link, exc_info=True)
    elif os.path.exists(link):
        raise FileExistsError(
            f"{link} exists and is not a symlink; refusing to replace it"
        )
    os.symlink(target, link)


def _drain(fd: int) -> None:
    try:
        while os.read(fd, 4096):
            pass
    except OSError:
        pass
