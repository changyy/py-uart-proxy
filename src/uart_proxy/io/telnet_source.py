"""
A telnet server as the device: ``--port telnet://host[:port]`` (SPEC S36).

For a BBS (``telnet://ptt.cc``), network gear with only a telnet CLI, or a
console server in plain telnet mode. Unlike raw TCP (``socket://``), telnet
interleaves **option negotiation** with the data — ``IAC`` (0xFF) commands —
which would otherwise show up as noise and, unanswered, leave many servers
waiting in line mode.

:class:`TelnetProtocol` is the protocol with no I/O, so it can be tested byte by
byte; :class:`TelnetSource` puts it on a socket. The negotiation is the minimal
useful one (RFC 854/855, with RFC 1143's rule that we only answer a *change*, so
two sides can never loop):

* we let the server ``ECHO`` and suppress go-ahead (``SGA``) — character at a
  time, which is what a BBS or a shell expects — and accept ``BINARY``;
* we offer our window size (``NAWS``, RFC 1073) and terminal type (``TTYPE``,
  RFC 1091, ``XTERM-256COLOR``) when asked, and send NAWS again on a resize —
  the same window-size story as ``ssh://`` (SPEC S35);
* everything else is refused.

Data: ``IAC IAC`` is a literal 0xFF both ways; outside binary mode a bare CR is
sent as ``CR NUL`` (RFC 854) and ``CR NUL`` received is a CR.
"""

from __future__ import annotations

import logging
import select
import socket
from typing import Optional
from urllib.parse import urlsplit

from .source import DataSource

logger = logging.getLogger(__name__)

IAC, DONT, DO, WONT, WILL, SB, SE = 255, 254, 253, 252, 251, 250, 240
NOP, GA = 241, 249
BINARY, ECHO, SGA, TTYPE, NAWS = 0, 1, 3, 24, 31
TTYPE_IS, TTYPE_SEND = 0, 1

DEFAULT_PORT = 23
TERMINAL_TYPE = b"XTERM-256COLOR"

#: Options the server may enable on its side (it sends WILL; we answer DO).
_THEY_MAY = {ECHO, SGA, BINARY}
#: Options we will enable on our side when asked (DO; we answer WILL).
_WE_WILL = {NAWS, TTYPE, SGA, BINARY}


class TelnetProtocol:
    """Telnet framing and negotiation, with no I/O."""

    def __init__(self, size: tuple[int, int] = (80, 24)) -> None:
        self.size = size
        self.us: set[int] = set()     # options enabled on our side
        self.them: set[int] = set()   # options enabled on theirs
        self._state = "data"
        self._verb = 0
        self._sb: bytearray = bytearray()
        self._last_cr = False         # a CR ended the previous chunk

    # ── receiving ───────────────────────────────────────────────────────────

    def receive(self, chunk: bytes) -> tuple[bytes, bytes]:
        """Split what arrived into (data for the session, replies to send)."""
        data = bytearray()
        replies = bytearray()
        for byte in chunk:
            state = self._state
            if state == "data":
                if byte == IAC:
                    self._state = "iac"
                elif self._last_cr and byte == 0 and BINARY not in self.them:
                    self._last_cr = False       # CR NUL is just a CR
                else:
                    data.append(byte)
                    self._last_cr = byte == 0x0D
            elif state == "iac":
                if byte == IAC:                  # an escaped 0xFF
                    data.append(IAC)
                    self._state = "data"
                elif byte in (WILL, WONT, DO, DONT):
                    self._verb, self._state = byte, "option"
                elif byte == SB:
                    self._sb.clear()
                    self._state = "sb"
                else:                            # NOP, GA, AYT, … : nothing to do
                    self._state = "data"
            elif state == "option":
                replies += self._negotiate(self._verb, byte)
                self._state = "data"
            elif state == "sb":
                if byte == IAC:
                    self._state = "sb-iac"
                else:
                    self._sb.append(byte)
            elif state == "sb-iac":
                if byte == SE:
                    replies += self._subnegotiation(bytes(self._sb))
                    self._state = "data"
                else:                            # IAC IAC inside SB is a 0xFF
                    self._sb.append(byte)
                    self._state = "sb"
        return bytes(data), bytes(replies)

    def _negotiate(self, verb: int, option: int) -> bytes:
        # RFC 1143: answer only a change of state, never re-confirm one —
        # otherwise two sides that each echo the other's answer loop forever.
        if verb == WILL:
            if option in self.them:
                return b""
            if option in _THEY_MAY:
                self.them.add(option)
                return bytes([IAC, DO, option])
            return bytes([IAC, DONT, option])
        if verb == WONT:
            if option not in self.them:
                return b""
            self.them.discard(option)
            return bytes([IAC, DONT, option])
        if verb == DO:
            if option in self.us:
                return b""
            if option in _WE_WILL:
                self.us.add(option)
                reply = bytes([IAC, WILL, option])
                if option == NAWS:
                    reply += self.naws()
                return reply
            return bytes([IAC, WONT, option])
        # DONT
        if option not in self.us:
            return b""
        self.us.discard(option)
        return bytes([IAC, WONT, option])

    def _subnegotiation(self, body: bytes) -> bytes:
        if body[:2] == bytes([TTYPE, TTYPE_SEND]) and TTYPE in self.us:
            return bytes([IAC, SB, TTYPE, TTYPE_IS]) + TERMINAL_TYPE + bytes([IAC, SE])
        return b""

    # ── sending ─────────────────────────────────────────────────────────────

    def encode(self, data: bytes) -> bytes:
        """Frame data for the wire: double IAC; a bare CR as CR NUL."""
        binary = BINARY in self.us
        out = bytearray()
        for i, byte in enumerate(data):
            if byte == IAC:
                out += bytes([IAC, IAC])
                continue
            out.append(byte)
            if byte == 0x0D and not binary:
                following = data[i + 1] if i + 1 < len(data) else None
                if following != 0x0A:
                    out.append(0)
        return bytes(out)

    def naws(self) -> bytes:
        """``IAC SB NAWS w h IAC SE``, with 255s in the sizes escaped."""
        cols, rows = self.size
        body = bytearray()
        for value in (cols, rows):
            for b in (value >> 8 & 0xFF, value & 0xFF):
                body.append(b)
                if b == IAC:
                    body.append(IAC)
        return bytes([IAC, SB, NAWS]) + bytes(body) + bytes([IAC, SE])

    def resize(self, cols: int, rows: int) -> bytes:
        """New size; the NAWS to send, if the server asked for it."""
        self.size = (cols, rows)
        return self.naws() if NAWS in self.us else b""


def parse_telnet_url(url: str) -> tuple[str, int]:
    parts = urlsplit(url)
    if parts.scheme != "telnet" or not parts.hostname:
        raise ValueError(f"telnet URL {url!r} needs a host, e.g. telnet://host:23")
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"telnet URL {url!r} has a bad port") from None
    return parts.hostname, port or DEFAULT_PORT


class TelnetSource(DataSource):
    def __init__(self, url: str, *, size: Optional[tuple[int, int]] = None,
                 connect_timeout: float = 10.0) -> None:
        self._url = url
        self._host, self._port = parse_telnet_url(url)
        self.fixed_size = size is not None
        self._initial = size or (80, 24)
        self._timeout = connect_timeout
        self._sock: Optional[socket.socket] = None
        self._proto = TelnetProtocol(self._initial)
        # Parity with UartSource, for the reporters that ask.
        self.is_exclusive = False
        self.busy = False

    @property
    def device_path(self) -> str:
        return self._url

    @property
    def size(self) -> tuple[int, int]:
        return self._proto.size

    @property
    def protocol(self) -> TelnetProtocol:
        return self._proto

    def open(self) -> None:
        sock = socket.create_connection((self._host, self._port), timeout=self._timeout)
        sock.setblocking(False)
        # A fresh negotiation per connection, keeping the size we last had.
        self._proto = TelnetProtocol(self._proto.size)
        self._sock = sock

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def read(self, max_bytes: int, timeout: float) -> bytes:
        sock = self._sock
        if sock is None:
            raise IOError("not connected")
        ready, _, _ = select.select([sock], [], [], timeout)
        if not ready:
            return b""
        try:
            chunk = sock.recv(max_bytes)
        except BlockingIOError:
            return b""
        if not chunk:
            raise IOError("telnet server closed the connection")
        data, replies = self._proto.receive(chunk)
        if replies:
            self._send(replies)
        return data

    def write(self, data: bytes) -> int:
        if self._sock is None:
            raise IOError("not connected")
        self._send(self._proto.encode(data))
        return len(data)

    def _send(self, raw: bytes) -> None:
        sock = self._sock
        if sock is None:
            return
        view = memoryview(raw)
        while view:
            try:
                sent = sock.send(view)
            except BlockingIOError:
                select.select([], [sock], [], 1.0)
                continue
            view = view[sent:]

    def set_window_size(self, cols: int, rows: int) -> None:
        """Send NAWS on a resize — unless the size is fixed (``--term-size``)."""
        if self.fixed_size or cols < 2 or rows < 2:
            return
        update = self._proto.resize(cols, rows)
        if update:
            try:
                self._send(update)
            except OSError:
                logger.debug("could not send NAWS", exc_info=True)

    def description(self) -> str:
        cols, rows = self._proto.size
        port = "" if self._port == DEFAULT_PORT else f":{self._port}"
        return f"telnet {self._host}{port} ({cols}×{rows})"
