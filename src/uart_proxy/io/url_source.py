"""
A serial port on the network, by URL — for console servers, ser2net, QEMU.

``--port socket://host:port`` or ``--port rfc2217://host:port`` (SPEC S34).
Lab console servers (Moxa, Digi, Lantronix, Cisco), ``ser2net``, Wi-Fi–serial
bridges and emulators (``qemu -serial tcp::4444,server``) all expose a UART as a
TCP port; this makes one the session's device, so everything else — timestamps,
the log files, plugins, search, mirrors, the proxy, background sessions — works
on it unchanged.

Both schemes come from pyserial's ``serial_for_url``:

* ``socket://`` is **raw TCP**: bytes in, bytes out. The serial settings live on
  the server; ours are not sent, and the description says so.
* ``rfc2217://`` is **Telnet with RFC 2217's COM-port control**: baud, framing
  and DTR/RTS are set on the far port, so it behaves like a local one.

Some things belong to a local device and do not apply here: there is no
``TIOCEXCL`` to take (the server decides who may connect), no busy port to
diagnose, no USB identity to follow. A dropped connection is an ordinary
failure, so S12's reconnect loop reconnects when the server is back.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlsplit

from uart_helper import UARTConfig

from .source import DataSource

logger = logging.getLogger(__name__)

#: The schemes accepted as a ``--port``: what each means for the settings.
SCHEMES = {
    "socket": "raw TCP — serial settings are the server's",
    "rfc2217": "Telnet + RFC 2217 — serial settings are applied remotely",
    "ssh": "OpenSSH — keys, known_hosts and ~/.ssh/config apply",
}


def is_port_url(port: Optional[str]) -> bool:
    """Whether ``--port`` names a network port rather than a device path."""
    return bool(port) and "://" in port


def check_port_url(url: str) -> Optional[str]:
    """An error message if ``url`` is not one we can open, else None."""
    parts = urlsplit(url)
    if parts.scheme not in SCHEMES:
        known = ", ".join(f"{s}://" for s in SCHEMES)
        return f"unsupported port URL {url!r}: use {known}"
    try:
        port = parts.port
    except ValueError:
        return f"port URL {url!r} has a bad port"
    if parts.scheme == "ssh":  # the port is ssh's business (default 22, config)
        return None if parts.hostname else f"ssh URL {url!r} needs a host, e.g. ssh://user@host"
    if not parts.hostname or port is None:
        return f"port URL {url!r} needs a host and a port, e.g. {parts.scheme}://host:4001"
    return None


class UrlSource(DataSource):
    """A pyserial URL port driven like any other source."""

    def __init__(self, url: str, config: Optional[UARTConfig] = None) -> None:
        self._url = url
        self._config = config or UARTConfig()
        self._scheme = urlsplit(url).scheme
        self._serial = None
        # Parity with UartSource, for the reporters that ask.
        self.is_exclusive = False
        self.busy = False

    @property
    def url(self) -> str:
        return self._url

    @property
    def device_path(self) -> str:
        return self._url

    @property
    def applies_settings(self) -> bool:
        return self._scheme == "rfc2217"

    def open(self) -> None:
        import serial

        cfg = self._config
        options = dict(
            baudrate=cfg.baudrate,
            bytesize=cfg.bytesize,
            parity=cfg.parity,
            stopbits=cfg.stopbits,
            xonxoff=cfg.xonxoff,
            rtscts=cfg.rtscts,
            dsrdtr=cfg.dsrdtr,
            timeout=0.2,
        )
        # pyserial's RFC 2217 client raises NotImplementedError for any
        # write_timeout at all, so only raw TCP gets one.
        if self._scheme == "socket":
            options["write_timeout"] = cfg.write_timeout
        port = serial.serial_for_url(self._url, do_not_open=True, **options)
        # pyserial's open() ends by flushing input — for a socket, discarding
        # whatever has already arrived. A console server's banner, a `login:`
        # prompt, sent the moment we connect, would then vanish whenever it
        # beat the flush: a race that loses the first thing the device says.
        # What the far end sends after we connect is output, not stale input.
        port.reset_input_buffer = lambda: None
        try:
            port.open()  # raises SerialException when the server is not there
        finally:
            del port.reset_input_buffer  # the class's own again
        self._serial = port

    def close(self) -> None:
        port, self._serial = self._serial, None
        if port is not None:
            try:
                port.close()
            except Exception:  # noqa: BLE001 - closing a dead socket
                logger.debug("closing %s", self._url, exc_info=True)

    def read(self, max_bytes: int, timeout: float) -> bytes:
        port = self._serial
        if port is None:
            raise IOError("not connected")
        # pyserial raises SerialException ("socket disconnected") when the far
        # end goes away: that is what lets the session reconnect.
        waiting = port.in_waiting
        if waiting:
            return port.read(min(waiting, max_bytes))
        # Only when it changes: setting it reconfigures an open port, which
        # for rfc2217 means resending baud and framing to the far end.
        if port.timeout != timeout:
            port.timeout = timeout
        return port.read(1)

    def write(self, data: bytes) -> int:
        port = self._serial
        if port is None:
            raise IOError("not connected")
        written = port.write(data)
        return len(data) if written is None else written

    def description(self) -> str:
        cfg = self._config
        framing = f"{cfg.baudrate} {cfg.bytesize}{cfg.parity}{int(cfg.stopbits)}"
        if self.applies_settings:
            return f"{self._url} @ {framing}"
        return f"{self._url} (raw TCP; settings are the server's)"
