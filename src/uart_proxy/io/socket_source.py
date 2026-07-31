"""
Remote transport: connect to another machine's uart-proxy server.

From the session's point of view this behaves exactly like a local UART: it
yields RX bytes and accepts TX bytes. Under the hood it speaks the JSON-lines
proxy protocol — authenticating on open, reconstructing the device's byte
stream from incoming ``rx`` messages, and wrapping outgoing bytes in ``tx``
messages.

Notices/status from the server are currently ignored here (the local session
produces its own); only the reconstructed device byte stream flows through.

Replay is the exception. When ``replay_lines`` is set, ``open()`` collects the
history the server sends before any live traffic and leaves it in
:attr:`SocketSource.replay` for the UI to render. It deliberately does **not**
flow into the session: those lines were assembled, stamped, recorded and passed
to plugins on the server already, so pushing them through a second time would
re-stamp them with the wrong time, duplicate them into this client's own log, and
fire every grep rule again on output from an hour ago.
"""

from __future__ import annotations

import socket
import time

from ..core.replay import ReplayEntry
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
        replay_lines: int = 0,
        replay_timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._auth_code = auth_code
        self._connect_timeout = connect_timeout
        self._replay_lines = replay_lines
        self._replay_timeout = replay_timeout

        self._sock: socket.socket | None = None
        self._recv_buf = bytearray()
        self._pending = bytearray()  # reconstructed device bytes awaiting read()
        self.role: Role | None = None
        self.remote_source_desc = ""
        #: History the server sent on attach, oldest first. Display-only.
        self.replay: list[ReplayEntry] = []
        #: How many lines the server said it could offer.
        self.replay_available = 0
        #: The server's own elapsed time at the moment we authenticated, so the
        #: client can share its timeline rather than starting a second one.
        self.remote_elapsed: float | None = None

    def open(self) -> None:
        # Idempotent: `attach` connects up front so the replayed history is in
        # hand before the UI's first frame, and the session's connection manager
        # then calls this again. Reconnecting after a drop still works, because
        # close() clears the socket.
        if self._sock is not None:
            return
        sock = socket.create_connection(
            (self._host, self._port), timeout=self._connect_timeout
        )
        self._sock = sock
        hello: dict = {"type": "auth", "code": self._auth_code}
        if self._replay_lines > 0:
            hello["replay"] = self._replay_lines
        sock.sendall(encode_message(hello))

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
        self.replay_available = int(msg.get("replay_available") or 0)
        if msg.get("elapsed") is not None:
            try:
                self.remote_elapsed = float(msg["elapsed"])
            except (TypeError, ValueError):
                self.remote_elapsed = None
        if self._replay_lines > 0:
            self._collect_replay()
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
            self._handle_live(msg)

    def _handle_live(self, msg: dict) -> None:
        if msg.get("type") == "rx":
            hex_str = msg.get("hex", "")
            if hex_str:
                try:
                    self._pending.extend(bytes.fromhex(hex_str))
                except ValueError:
                    pass

    def _collect_replay(self) -> None:
        """Drain the history block the server promised, up to ``replay_end``.

        A server that doesn't know about replay simply never sends it; rather
        than hang, give up after ``replay_timeout`` and carry on live — a missing
        history is a worse view, not a broken one. Anything else that arrives
        meanwhile (an early ``rx``, a ``status``) is kept for the live stream.
        """
        deadline = time.monotonic() + self._replay_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            line = self._read_one_line(timeout=remaining)
            if line is None:
                return
            if not line.strip():
                continue
            try:
                msg = decode_message(line)
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "replay":
                self.replay.append(ReplayEntry.from_message(msg))
            elif mtype == "replay_end":
                return
            else:
                self._handle_live(msg)

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
