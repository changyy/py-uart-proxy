"""
Local UART transport, backed by ``uart_helper.UARTDevice``.

This is the only source that actually opens a physical serial port. On macOS
the ``/dev/cu.*`` path that pyserial enumerates is rewritten to the matching
``/dev/tty.*`` path (what ``screen`` uses) for reliable bidirectional traffic.
"""

from __future__ import annotations

from typing import Optional

from uart_helper import PortIdentity, UARTConfig, UARTDevice

from .source import DataSource


class UartSource(DataSource):
    def __init__(self, device: str, config: Optional[UARTConfig] = None) -> None:
        # PortIdentity.tty_device maps /dev/cu.* -> /dev/tty.* on macOS and is a
        # no-op elsewhere.
        identity = PortIdentity(device=device)
        self._device_path = identity.tty_device
        self._config = config or UARTConfig()
        self._dev = UARTDevice(PortIdentity(device=self._device_path), self._config)

    def open(self) -> None:
        self._dev.open()

    def close(self) -> None:
        self._dev.close()

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
