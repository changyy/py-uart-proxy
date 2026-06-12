"""
Remote transport: connect to another machine's uart-proxy server.

From the session's point of view this behaves exactly like a local UART: it
yields RX bytes and accepts TX bytes. Under the hood it speaks the JSON-lines
proxy protocol — authenticating on open, reconstructing the device's byte
stream from incoming ``rx`` messages, and wrapping outgoing bytes in ``tx``
messages.

Notices/status from the server are currently ignored here (the local session
produces its own); only the reconstructed device byte stream flows through.
"""

from __future__ import annotations

import socket
from collections import deque

from ..proxy.protocol import Role, decode_message, encode_message
from .source import DataSource


class SocketSourceError(Exception):
    """Raised when the remote connection cannot be established or authed."""


class SocketSource(DataSource):
    def __init__(
        self,
        host: str,
        port: int,
        auth_code: str,
        *,
        connect_timeout: float = 5.0,
    ) -> None:
        self._host = host
        self._port = port
        self._auth_code = auth_code
        self._connect_timeout = connect_timeout

        self._sock: socket.socket | None = None
        self._recv_buf = bytearray()
        self._pending = bytearray()  # reconstructed device bytes awaiting read()
        self.role: Role | None = None
        self.remote_source_desc = ""

    def open(self) -> None:
        sock = socket.create_connection(
            (self._host, self._port), timeout=self._connect_timeout
        )
        self._sock = sock
        sock.sendall(encode_message({"type": "auth", "code": self._auth_code}))

        # Read the single auth response line (still in blocking-with-timeout mode).
        reply = self._read_one_line(timeout=self._connect_timeout)
        if reply is None:
            self.close()
            raise SocketSourceError("no response from server during auth")
        msg = decode_message(reply)
        if msg.get("type") != "auth_ok":
            reason = msg.get("reason", "authentication failed")
            self.close()
            raise SocketSourceError(str(reason))

        role_str = msg.get("role", Role.FULL.value)
        try:
            self.role = Role(role_str)
        except ValueError:
            self.role = Role.READONLY
        self.remote_source_desc = msg.get("source", "")
        sock.settimeout(0.2)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def read(self, max_bytes: int, timeout: float) -> bytes:
        if self._sock is None:
            raise SocketSourceError("not connected")

        # If we already have reconstructed bytes buffered, return them now.
        if not self._pending:
            self._sock.settimeout(timeout)
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                # No data within the timeout — NOT an EOF. (recv() only returns
                # b"" on an orderly peer shutdown, never on a timeout.)
                chunk = None
            except OSError as exc:
                raise SocketSourceError(f"connection error: {exc}") from exc
            if chunk == b"":
                raise SocketSourceError("server closed the connection")
            if chunk:
                self._ingest(chunk)

        if not self._pending:
            return b""
        out = bytes(self._pending[:max_bytes])
        del self._pending[: len(out)]
        return out

    def write(self, data: bytes) -> int:
        if self._sock is None:
            raise SocketSourceError("not connected")
        if self.role == Role.READONLY:
            raise SocketSourceError("remote session is read-only; writes are not allowed")
        self._sock.sendall(encode_message({"type": "tx", "hex": data.hex()}))
        return len(data)

    def description(self) -> str:
        base = f"remote {self._host}:{self._port}"
        if self.remote_source_desc:
            base += f" → {self.remote_source_desc}"
        if self.role is not None:
            base += f" [{self.role.value}]"
        return base

    @property
    def writable(self) -> bool:
        return self.role != Role.READONLY

    # ── internals ────────────────────────────────────────────────────────────

    def _ingest(self, chunk: bytes) -> None:
        """Split incoming bytes into protocol lines and reconstruct rx bytes."""
        self._recv_buf.extend(chunk)
        while True:
            idx = self._recv_buf.find(b"\n")
            if idx < 0:
                break
            line = bytes(self._recv_buf[:idx])
            del self._recv_buf[: idx + 1]
            if not line.strip():
                continue
            try:
                msg = decode_message(line)
            except ValueError:
                continue
            if msg.get("type") == "rx":
                hex_str = msg.get("hex", "")
                if hex_str:
                    try:
                        self._pending.extend(bytes.fromhex(hex_str))
                    except ValueError:
                        pass

    def _read_one_line(self, timeout: float) -> bytes | None:
        """Block (up to ``timeout``) for a single complete protocol line."""
        assert self._sock is not None
        self._sock.settimeout(timeout)
        while True:
            idx = self._recv_buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._recv_buf[:idx])
                del self._recv_buf[: idx + 1]
                return line
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self._recv_buf.extend(chunk)
