"""
The socket proxy server.

Exposes a running :class:`~uart_proxy.core.session.UartSession` to remote
clients. It subscribes to the session bus and fans RX/notice/status events out
to every authenticated client; clients with the ``full`` role may send ``tx``
messages which are written back to the session (and thus the real UART).

Threading model (kept off the asyncio/TUI loop on purpose):

* one accept thread,
* one reader thread per client (parses client → server messages),
* one writer thread per client (drains that client's send queue to the socket).

The bus callback runs in the session's read thread and only enqueues bytes, so
a slow client can never stall the serial pump.
"""

from __future__ import annotations

import logging
import queue
import socket
import threading
from typing import TYPE_CHECKING, Optional

from ..core.events import Direction, Event, EventKind
from .protocol import EOL_MAP, Role, decode_message, encode_message

if TYPE_CHECKING:  # avoid a circular import; only needed for type hints
    from ..core.session import UartSession

logger = logging.getLogger(__name__)

_AUTH_TIMEOUT = 10.0       # seconds a client has to send its auth line
_SEND_QUEUE_MAX = 10000    # per-client backlog before we drop the slowest client


class _Client:
    def __init__(self, conn: socket.socket, addr) -> None:
        self.conn = conn
        self.addr = addr
        self.role: Optional[Role] = None
        self.send_q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=_SEND_QUEUE_MAX)
        self._recv_buf = bytearray()
        self.writer_thread: Optional[threading.Thread] = None

    def enqueue(self, line: bytes) -> bool:
        try:
            self.send_q.put_nowait(line)
            return True
        except queue.Full:
            return False

    def start_writer(self) -> None:
        self.writer_thread = threading.Thread(
            target=self._writer_loop, name=f"client-writer-{self.addr}", daemon=True
        )
        self.writer_thread.start()

    def _writer_loop(self) -> None:
        while True:
            line = self.send_q.get()
            if line is None:  # sentinel => shut down
                break
            try:
                self.conn.sendall(line)
            except OSError:
                break

    def shutdown(self) -> None:
        try:
            self.send_q.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.conn.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def read_lines(self):
        """Yield complete protocol lines from the client until disconnect."""
        while True:
            idx = self._recv_buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._recv_buf[:idx])
                del self._recv_buf[: idx + 1]
                yield line
                continue
            try:
                chunk = self.conn.recv(65536)
            except (OSError, socket.timeout):
                return
            if not chunk:
                return
            self._recv_buf.extend(chunk)


class ProxyServer:
    def __init__(
        self,
        session: "UartSession",
        auth: dict[str, Role],
        *,
        host: str = "0.0.0.0",
        port: int = 9600,
    ) -> None:
        self.session = session
        self.auth = auth
        self.host = host
        self.port = port

        self._srv: Optional[socket.socket] = None
        self._accept_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._clients: set[_Client] = set()
        self._clients_lock = threading.Lock()
        self._unsubscribe = None

    # ── lifecycle ──────────────────────────────────────────────────────────

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        # Reflect the actually-bound port (supports port=0 for ephemeral ports).
        self.port = srv.getsockname()[1]
        srv.listen(16)
        srv.settimeout(0.5)
        self._srv = srv
        self._unsubscribe = self.session.bus.subscribe(self._on_event)
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="proxy-accept", daemon=True
        )
        self._accept_thread.start()
        logger.info("Proxy server listening on %s:%d", self.host, self.port)

    def stop(self) -> None:
        self._stop.set()
        if self._unsubscribe is not None:
            self._unsubscribe()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        with self._clients_lock:
            clients = list(self._clients)
            self._clients.clear()
        for client in clients:
            client.shutdown()

    @property
    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

    # ── accept / auth ────────────────────────────────────────────────────────

    def _accept_loop(self) -> None:
        assert self._srv is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self._handle_client, args=(conn, addr), daemon=True
            ).start()

    def _handle_client(self, conn: socket.socket, addr) -> None:
        client = _Client(conn, addr)
        try:
            conn.settimeout(_AUTH_TIMEOUT)
            if not self._authenticate(client):
                return
            conn.settimeout(None)
            with self._clients_lock:
                self._clients.add(client)
            client.start_writer()
            logger.info("Client %s authenticated as %s", addr, client.role.value)
            self._reader_loop(client)
        except Exception:  # noqa: BLE001
            logger.debug("Client %s handler error", addr, exc_info=True)
        finally:
            with self._clients_lock:
                self._clients.discard(client)
            client.shutdown()
            logger.info("Client %s disconnected", addr)

    def _authenticate(self, client: _Client) -> bool:
        for line in client.read_lines():
            if not line.strip():
                continue
            try:
                msg = decode_message(line)
            except ValueError:
                self._send_now(client, {"type": "auth_fail", "reason": "bad message"})
                return False
            if msg.get("type") != "auth":
                self._send_now(client, {"type": "auth_fail", "reason": "expected auth"})
                return False
            code = str(msg.get("code", ""))
            role = self.auth.get(code)
            if role is None:
                self._send_now(client, {"type": "auth_fail", "reason": "invalid code"})
                return False
            client.role = role
            self._send_now(
                client,
                {
                    "type": "auth_ok",
                    "role": role.value,
                    "source": self.session.source.description(),
                },
            )
            return True
        return False  # disconnected before sending auth

    # ── client → server ──────────────────────────────────────────────────────

    def _reader_loop(self, client: _Client) -> None:
        for line in client.read_lines():
            if not line.strip():
                continue
            try:
                msg = decode_message(line)
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "tx":
                self._handle_tx(client, msg)
            elif mtype == "ping":
                client.enqueue(encode_message({"type": "pong"}))

    def _handle_tx(self, client: _Client, msg: dict) -> None:
        if client.role != Role.FULL:
            client.enqueue(
                encode_message({"type": "notice", "text": "write denied (read-only)"})
            )
            return
        data: bytes
        if "hex" in msg:
            try:
                data = bytes.fromhex(msg["hex"])
            except ValueError:
                return
        elif "text" in msg:
            eol = EOL_MAP.get(str(msg.get("eol", "crlf")), b"\r\n")
            data = str(msg["text"]).encode("utf-8", errors="replace") + eol
        else:
            return
        try:
            self.session.write(data)
        except Exception as exc:  # noqa: BLE001
            client.enqueue(encode_message({"type": "notice", "text": f"write failed: {exc}"}))

    # ── server → client (bus fan-out) ─────────────────────────────────────────

    def _on_event(self, event: Event) -> None:
        msg = self._serialize(event)
        if msg is None:
            return
        line = encode_message(msg)
        with self._clients_lock:
            clients = list(self._clients)
        for client in clients:
            if not client.enqueue(line):
                # Backlog full: the client can't keep up — drop it.
                logger.warning("Dropping slow client %s", client.addr)
                client.shutdown()
                with self._clients_lock:
                    self._clients.discard(client)

    @staticmethod
    def _serialize(event: Event) -> Optional[dict]:
        if event.kind == EventKind.DATA and event.direction == Direction.RX:
            return {
                "type": "rx",
                "seq": event.seq,
                "wall": event.stamp.wall_str(),
                "elapsed": round(event.stamp.elapsed, 4),
                "hex": event.data.hex(),
                "text": event.text,
            }
        if event.kind == EventKind.NOTICE:
            return {"type": "notice", "text": event.text, "meta": event.meta}
        if event.kind == EventKind.STATUS:
            return {"type": "status", "state": event.text, "meta": event.meta}
        return None

    def _send_now(self, client: _Client, msg: dict) -> None:
        try:
            client.conn.sendall(encode_message(msg))
        except OSError:
            pass
