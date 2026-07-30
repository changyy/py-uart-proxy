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
from typing import Optional

from uart_helper import PortIdentity, UARTConfig, UARTDevice

from .source import DataSource

logger = logging.getLogger(__name__)

#: TIOCEXCL is missing from ``termios`` on some builds; these are the values the
#: two platforms we support actually use.
_TIOCEXCL_FALLBACK = {"darwin": 0x2000740D, "linux": 0x540C}


def seize_exclusive(fd: int) -> bool:
    """Claim an open tty ``fd`` exclusively, so no one else can open the device.

    Issues ``TIOCEXCL``, which is **kernel-enforced**: every later ``open()`` of
    that device path fails with ``EBUSY`` until we close it. That is what makes
    ``screen /dev/cu.usbserial-110`` refuse to start while uart-proxy holds the
    port, instead of both processes silently splitting the byte stream between
    them — which is worse than an error, because nothing reports it.

    Note this is *not* what pyserial's ``exclusive=True`` does: that takes an
    ``flock``, which is advisory and only blocks other programs that also
    ``flock``. ``screen`` does not, so ``flock`` alone would not stop it.

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


class UartSource(DataSource):
    def __init__(
        self,
        device: str,
        config: Optional[UARTConfig] = None,
        *,
        exclusive: bool = True,
    ) -> None:
        # PortIdentity.tty_device maps /dev/cu.* -> /dev/tty.* on macOS and is a
        # no-op elsewhere.
        identity = PortIdentity(device=device)
        self._device_path = identity.tty_device
        self._config = config or UARTConfig()
        self._exclusive = exclusive
        self._dev = UARTDevice(PortIdentity(device=self._device_path), self._config)
        self.is_exclusive = False  # what we actually got, for status display

    def open(self) -> None:
        self._dev.open()
        self.is_exclusive = False
        if self._exclusive:
            fd = self._fileno()
            if fd is not None:
                self.is_exclusive = seize_exclusive(fd)

    def close(self) -> None:
        self._dev.close()
        self.is_exclusive = False

    def read(self, max_bytes: int, timeout: float) -> bytes:
        # Drain whatever is already buffered for responsiveness; otherwise do a
        # short blocking read so the loop stays cheap when the line is quiet.
        waiting = self._dev.in_waiting
        if waiting:
            result = self._dev.read(min(waiting, max_bytes))
            return result.data
        result = self._dev.read(1, timeout_ms=int(timeout * 1000))
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
