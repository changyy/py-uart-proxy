"""
Local UART transport, backed by ``uart_helper.UARTDevice``.

This is the only source that actually opens a physical serial port. On macOS
the ``/dev/cu.*`` path that pyserial enumerates is rewritten to the matching
``/dev/tty.*`` path (what ``screen`` uses) for reliable bidirectional traffic.

The port is claimed **exclusively** by default (see :func:`seize_exclusive`), so
a second program cannot quietly start stealing bytes from the same wire.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Optional

from uart_helper import PortIdentity, UARTConfig, UARTDevice

from ..core.port_busy import is_busy_error, same_device
from ..core.port_identity import find_moved, has_usb_identity
from .source import DataSource

logger = logging.getLogger(__name__)

#: ``TransferResult.error_code`` for a read that timed out (uart_helper).
_READ_TIMEOUT = 2

#: TIOCEXCL is missing from ``termios`` on some builds; these are the values the
#: two platforms we support actually use.
_TIOCEXCL_FALLBACK = {"darwin": 0x2000740D, "linux": 0x540C}
_TIOCNXCL_FALLBACK = {"darwin": 0x2000740E, "linux": 0x540D}


def seize_exclusive(fd: int) -> bool:
    """Claim an open tty ``fd`` exclusively, so no one else can open the device.

    Issues ``TIOCEXCL``, which is **kernel-enforced**: every later ``open()`` of
    that device path fails with ``EBUSY`` until we close it. Without it a second
    open of the same node *succeeds* on POSIX, and the two processes then split
    the byte stream between them — worse than an error, because nothing reports
    it. Measured behaviour, and which case this actually closes, is tabulated in
    SPEC S15.

    Note this is *not* what pyserial's ``exclusive=True`` does: that takes an
    ``flock``, which is advisory and only blocks other programs that also
    ``flock``.

    Returns True if the claim was made. Best-effort by design: Windows COM ports
    are already exclusive at the OS level, and a non-tty fd (a pipe in a test)
    has nothing to claim — neither case is an error.
    """
    if sys.platform == "win32":
        return False  # COM ports are exclusive-open already
    try:
        import fcntl
        import termios
    except ImportError:  # pragma: no cover - POSIX always has these
        return False

    request = getattr(termios, "TIOCEXCL", None) or _TIOCEXCL_FALLBACK.get(sys.platform)
    if request is None:
        logger.info("no TIOCEXCL for platform %s; port not claimed exclusively",
                    sys.platform)
        return False
    try:
        fcntl.ioctl(fd, request)
    except OSError as exc:
        logger.info("TIOCEXCL failed (%s); port not claimed exclusively", exc)
        return False
    return True


def release_exclusive(fd: int) -> bool:
    """Give up a :func:`seize_exclusive` claim, before closing ``fd``.

    Closing alone is not enough: the kernel clears ``TIOCEXCL`` only when the
    tty's last opener closes it. A pty whose other program keeps it open (socat,
    QEMU's ``-serial pty``) would stay ``EBUSY`` to every later open — ours
    included — until that program exits (SPEC S15). Best-effort, like the claim.
    """
    if sys.platform == "win32":
        return False
    try:
        import fcntl
        import termios
    except ImportError:  # pragma: no cover - POSIX always has these
        return False
    request = getattr(termios, "TIOCNXCL", None) or _TIOCNXCL_FALLBACK.get(sys.platform)
    if request is None:
        return False
    try:
        fcntl.ioctl(fd, request)
    except OSError as exc:
        logger.info("TIOCNXCL failed (%s)", exc)
        return False
    return True


def _scan_ports() -> list:
    from uart_helper import SerialMonitor

    return [ident for ident, _ in SerialMonitor().scan_once()]


class UartSource(DataSource):
    def __init__(
        self,
        device: str,
        config: Optional[UARTConfig] = None,
        *,
        exclusive: bool = True,
        scan: Optional[Callable[[], list]] = None,
    ) -> None:
        # PortIdentity.tty_device maps /dev/cu.* -> /dev/tty.* on macOS and is a
        # no-op elsewhere.
        identity = PortIdentity(device=device)
        self._device_path = identity.tty_device
        self._config = config or UARTConfig()
        self._exclusive = exclusive
        self._dev = UARTDevice(PortIdentity(device=self._device_path), self._config)
        self.is_exclusive = False  # what we actually got, for status display
        #: Whether the last open() failed because another process holds the
        #: port (SPEC S21), as opposed to it being absent or not permitted.
        self.busy = False
        # Following an adapter that re-enumerates under a new name (SPEC S31):
        # who it is, learnt at the first successful open, and how to look.
        self._scan = scan or _scan_ports
        self.identity = None
        #: Called with (old_path, new_path) when the adapter is found elsewhere.
        self.on_moved: Optional[Callable[[str, str], None]] = None

    def open(self) -> None:
        try:
            self._dev.open()
        except Exception as exc:
            self.busy = is_busy_error(exc)
            # Gone from its path — but maybe back under another name.
            if self.busy or not self._follow():
                raise
            self._dev.open()   # the new path; failing here is an ordinary failure
        self.busy = False
        if self.identity is None:
            self.identity = self._identify(self._device_path)
        self.is_exclusive = False
        if self._exclusive:
            fd = self._fileno()
            if fd is not None:
                self.is_exclusive = seize_exclusive(fd)

    def close(self) -> None:
        if self.is_exclusive:
            fd = self._fileno()
            if fd is not None:
                release_exclusive(fd)
        self._dev.close()
        self.is_exclusive = False

    def read(self, max_bytes: int, timeout: float) -> bytes:
        # Drain whatever is already buffered for responsiveness; otherwise do a
        # short blocking read so the loop stays cheap when the line is quiet.
        waiting = self._dev.in_waiting
        if waiting:
            result = self._dev.read(min(waiting, max_bytes))
        else:
            result = self._dev.read(1, timeout_ms=int(timeout * 1000))
        # uart_helper reports a failed read in the result instead of raising.
        # Returning its empty data would pass off an unplugged device as a
        # quiet one, and the session would never reconnect; a timeout is the
        # only failure that really means "nothing yet".
        if not result.ok and result.error_code != _READ_TIMEOUT:
            raise IOError(result.error_message or "UART read failed")
        return result.data

    def write(self, data: bytes) -> int:
        result = self._dev.write(data)
        if not result.ok:
            raise IOError(result.error_message or "UART write failed")
        return result.bytes_transferred

    def description(self) -> str:
        cfg = self._config
        return f"{self._device_path} @ {cfg.baudrate} {cfg.bytesize}{cfg.parity}{int(cfg.stopbits)}"

    @property
    def device_path(self) -> str:
        return self._device_path

    def _identify(self, path: str):
        """This port's USB identity, from a scan, or None if it has none."""
        try:
            for ident in self._scan():
                if same_device(ident.tty_device, path):
                    return ident if has_usb_identity(ident) else None
        except Exception:  # noqa: BLE001 - identity is a bonus, never a failure
            logger.debug("port scan failed", exc_info=True)
        return None

    def _follow(self) -> bool:
        """Switch to where the adapter is now, if it moved. True if switched."""
        if self.identity is None:
            return False
        try:
            candidates = self._scan()
        except Exception:  # noqa: BLE001
            return False
        new_path, why = find_moved(self.identity, candidates)
        if new_path is None or same_device(new_path, self._device_path):
            if why:
                logger.debug("not following %s: %s", self._device_path, why)
            return False
        old_path = self._device_path
        self._device_path = PortIdentity(device=new_path).tty_device
        self._dev = UARTDevice(PortIdentity(device=self._device_path), self._config)
        logger.info("%s re-enumerated as %s", old_path, self._device_path)
        if self.on_moved is not None:
            self.on_moved(old_path, self._device_path)
        return True

    def _fileno(self) -> Optional[int]:
        """The open port's file descriptor, or None if we can't reach it.

        ``UARTDevice`` wraps its ``serial.Serial`` privately and exposes no
        ``fileno()``, so we reach for it defensively — a future version that
        renames the attribute costs us the exclusive claim, not the session.
        (Worth proposing upstream: either ``fileno()`` or an ``exclusive`` flag
        on ``UARTConfig``. See ROADMAP.)
        """
        port = getattr(self._dev, "_serial", None)
        if port is None:
            return None
        try:
            return port.fileno()
        except Exception:  # noqa: BLE001 - closed / not a real port
            return None
