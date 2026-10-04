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
import time
from collections import deque
from typing import TYPE_CHECKING, Callable, Optional

from ..core.events import Direction, Event, EventKind
from ..core.replay import ReplayBuffer
from .protocol import EOL_MAP, Role, decode_message, encode_message

if TYPE_CHECKING:  # avoid a circular import; only needed for type hints
    from ..core.session import UartSession

logger = logging.getLogger(__name__)

_AUTH_TIMEOUT = 10.0       # seconds a client has to send its auth line
_SEND_QUEUE_MAX = 10000    # per-client backlog before we drop the slowest client

#: Failed auth attempts one address may make within ``FAIL_WINDOW`` seconds
#: before it is refused for ``BAN_SECONDS`` (SPEC S22). Per address, and for a
#: fixed time, rather than locking the server: a lockout for everyone is one a
#: stranger on the LAN can trigger at will, against the people meant to use it.
#: Connections served at once, authenticated or not (SPEC S26). Each one is two
#: threads and a send queue; unauthenticated ones count too, or idle sockets
#: that never send `auth` could hold every slot for their 10 s grace each.
MAX_CLIENTS = 16

#: How long a client's own name (``auth.client``, SPEC S40) may be.
CLIENT_NAME_MAX = 64

MAX_AUTH_FAILURES = 10
FAIL_WINDOW = 60.0
BAN_SECONDS = 600.0


def _wire_health(h: dict) -> dict:
    """The device's health as sent: its state, since when, error, reconnects, last output."""
    return {k: h[k] for k in ("state", "since", "since_epoch", "error", "reconnects",
                              "last_output", "last_output_epoch")}


class AuthLimiter:
    """Counts failed auth attempts per address and refuses repeat offenders."""

    def __init__(
        self,
        *,
        max_failures: int = MAX_AUTH_FAILURES,
        window: float = FAIL_WINDOW,
        ban: float = BAN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_failures = max_failures
        self.window = window
        self.ban = ban
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}
        self._banned_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def banned_for(self, address: str) -> float:
        """Seconds ``address`` is still refused for; 0 if it may try."""
        with self._lock:
            until = self._banned_until.get(address)
            if until is None:
                return 0.0
            remaining = until - self._clock()
            if remaining <= 0:
                del self._banned_until[address]
                return 0.0
            return remaining

    def record_failure(self, address: str) -> bool:
        """Count one failure; True if this one got ``address`` refused."""
        with self._lock:
            now = self._clock()
            recent = self._failures.setdefault(address, deque())
            recent.append(now)
            while recent and now - recent[0] > self.window:
                recent.popleft()
            if len(recent) < self.max_failures:
                return False
            del self._failures[address]
            self._banned_until[address] = now + self.ban
            return True

    def record_success(self, address: str) -> None:
        """A right code clears the slate: a typo or two is not an attack."""
        with self._lock:
            self._failures.pop(address, None)



class _Client:
    def __init__(self, conn: socket.socket, addr) -> None:
        self.conn = conn
        self.addr = addr
        self.role: Optional[Role] = None
        #: The name the client gave itself (``auth.client``), if any (S40).
        self.name = ""
        self.connected_at = time.time()
        #: The last message from this client, pings included (S45).
        self.last_seen = self.connected_at
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
        replay: Optional[ReplayBuffer] = None,
        limiter: Optional[AuthLimiter] = None,
        max_clients: int = MAX_CLIENTS,
        echo_tx: bool = False,
    ) -> None:
        self.session = session
        self.auth = auth
        self.limiter = limiter or AuthLimiter()
        self.max_clients = max_clients
        # Forward typed lines to clients as `tx_echo` (SPEC S29). Off by
        # default: a line typed at a password prompt is still a line.
        self.echo_tx = echo_tx
        # Which client's `tx` is being written right now, on this thread — so
        # its own line is not echoed back to it (it already shows it).
        self._origin = threading.local()
        self._active = 0          # connections being served, authed or not
        self._active_lock = threading.Lock()
        self.host = host
        self.port = port
        # Optional history, so an attaching client can be shown what it missed.
        self.replay = replay

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
            # On Linux close() alone leaves the socket listening until the
            # accept thread's poll returns, so the port cannot be bound again
            # at once; shutdown() takes it out of LISTEN and wakes that thread.
            try:
                self._srv.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # macOS: ENOTCONN on a listening socket
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

    def clients(self) -> list[dict]:
        """The authenticated connections, oldest first (SPEC S40)."""
        with self._clients_lock:
            clients = sorted(self._clients, key=lambda c: c.connected_at)
        return [{"address": self._endpoint(c), "role": c.role.value if c.role else "",
                 "client": c.name, "connected_at": c.connected_at, "last_seen": c.last_seen}
                for c in clients]

    @staticmethod
    def _endpoint(client: "_Client") -> str:
        addr = client.addr
        if isinstance(addr, tuple) and len(addr) >= 2:
            return f"{addr[0]}:{addr[1]}"
        return str(addr)

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
            with self._active_lock:
                full = self.max_clients > 0 and self._active >= self.max_clients
                if not full:
                    self._active += 1
            if full:
                self._refuse_full(conn)
                continue
            threading.Thread(
                target=self._serve_counted, args=(conn, addr), daemon=True
            ).start()

    def _serve_counted(self, conn: socket.socket, addr) -> None:
        try:
            self._handle_client(conn, addr)
        finally:
            with self._active_lock:
                self._active -= 1

    def _refuse_full(self, conn: socket.socket) -> None:
        """Turn a connection away at the cap, on the accept thread, quickly.

        ``retry: true`` because a full server is not a wrong code: the client
        should keep trying (S22's clients stop on any other ``auth_fail``).
        """
        try:
            conn.settimeout(1.0)
            conn.sendall(encode_message({
                "type": "auth_fail", "retry": True,
                "reason": f"server full ({self.max_clients} connections)",
            }))
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _handle_client(self, conn: socket.socket, addr) -> None:
        client = _Client(conn, addr)
        try:
            conn.settimeout(_AUTH_TIMEOUT)
            remaining = self.limiter.banned_for(self._address(client))
            if remaining:
                self._send_now(client, {
                    "type": "auth_fail",
                    "reason": f"too many failed attempts; try again in "
                              f"{int(remaining + 0.999)}s",
                })
                return
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

    @staticmethod
    def _address(client: _Client) -> str:
        addr = client.addr
        return str(addr[0]) if isinstance(addr, tuple) and addr else str(addr)

    def _reject(self, client: _Client, reason: str) -> bool:
        """Refuse this attempt, and count it against the client's address."""
        self._send_now(client, {"type": "auth_fail", "reason": reason})
        address = self._address(client)
        if self.limiter.record_failure(address):
            minutes = self.limiter.ban / 60
            self.session.publish_notice(
                f"proxy: refusing {address} for {minutes:g} min after "
                f"{self.limiter.max_failures} failed auth attempts"
            )
        return False

    def _authenticate(self, client: _Client) -> bool:
        for line in client.read_lines():
            if not line.strip():
                continue
            try:
                msg = decode_message(line)
            except ValueError:
                return self._reject(client, "bad message")
            if msg.get("type") != "auth":
                return self._reject(client, "expected auth")
            code = str(msg.get("code", ""))
            role = self.auth.get(code)
            if role is None:
                return self._reject(client, "invalid code")
            self.limiter.record_success(self._address(client))
            client.role = role
            client.name = str(msg.get("client") or "")[:CLIENT_NAME_MAX]
            # Live from this moment, under the lock the fan-out takes: whatever the
            # device says from now on waits in this client's queue (its writer
            # starts after the replay), and the replay is what came before. Added
            # only after the replay, output in between was lost.
            with self._clients_lock:
                self._clients.add(client)
                entries = self._replay_entries(msg.get("replay"))
            self._send_now(
                client,
                {
                    "type": "auth_ok",
                    "role": role.value,
                    "source": self.session.source.description(),
                    "replay_available": len(self.replay) if self.replay else 0,
                    # Where this session is on its own clock, so a client can
                    # adopt our timeline instead of starting a second one.
                    "elapsed": round(self.session.tracker.stamp().elapsed, 4),
                    # S43: the device's state now, so a client never assumes it.
                    "device": _wire_health(self.session.device_health()),
                },
            )
            self._send_replay(client, entries)
            return True
        return False  # disconnected before sending auth

    def _replay_entries(self, requested):
        """The history a client asked for (``None`` when it asked for none)."""
        if requested is None:
            return None
        try:
            limit = int(requested)
        except (TypeError, ValueError):
            return None
        if limit <= 0:
            return None
        return self.replay.snapshot(limit) if self.replay is not None else []

    def _send_replay(self, client: _Client, entries) -> None:
        """Send the history this client asked for, before any live traffic.

        Sent straight to the socket while the client's writer has not started:
        live messages queue behind it, so replayed lines are never interleaved
        with live ones — the client can rely on everything after
        ``replay_end`` being the present.

        Replayed lines go out as their own ``replay`` message type rather than as
        ``rx``: they carry the server's original stamps and must not be mistaken
        for what is happening now. A client that asks for no replay, or an older
        one that does not know the field, gets nothing and behaves exactly as
        before. One that asked gets ``replay_end`` even with nothing to send — a
        client waiting for it must not wait out its timeout.
        """
        if entries is None:
            return
        for entry in entries:
            self._send_now(client, entry.to_message())
        self._send_now(client, {
            "type": "replay_end",
            "count": len(entries),
            "from": entries[0].wall if entries else None,
            "to": entries[-1].wall if entries else None,
        })

    # ── client → server ──────────────────────────────────────────────────────

    def _reader_loop(self, client: _Client) -> None:
        for line in client.read_lines():
            if not line.strip():
                continue
            client.last_seen = time.time()
            try:
                msg = decode_message(line)
            except ValueError:
                continue
            mtype = msg.get("type")
            if mtype == "tx":
                self._handle_tx(client, msg)
            elif mtype == "ping":
                client.enqueue(encode_message({"type": "pong"}))
            elif mtype == "resize":
                self._handle_resize(client, msg)

    def _handle_resize(self, client: _Client, msg: dict) -> None:
        """A client's window size, for a device that can use one (SPEC S38).

        Only ``ssh://`` and ``telnet://`` sources can tell the far end; for a
        UART it is ignored. Full-access clients only — the size shapes what the
        device draws for everyone, and a read-only viewer may not change what
        the device does. The latest size wins, as when one person resizes a
        shared tmux window.
        """
        if client.role != Role.FULL:
            return
        tell = getattr(self.session.source, "set_window_size", None)
        if tell is None:
            return
        try:
            cols, rows = int(msg.get("cols")), int(msg.get("rows"))
        except (TypeError, ValueError):
            return
        if 2 <= cols <= 1000 and 2 <= rows <= 1000:
            tell(cols, rows)

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
        # The TX line event fires synchronously inside write(), on this thread.
        self._origin.client = client
        try:
            self.session.write(data, origin={
                "via": "proxy", "role": client.role.value,
                "client": client.name, "address": self._endpoint(client),
            })
        except Exception as exc:  # noqa: BLE001
            client.enqueue(encode_message({"type": "notice", "text": f"write failed: {exc}"}))
        finally:
            self._origin.client = None

    # ── server → client (bus fan-out) ─────────────────────────────────────────

    def _on_event(self, event: Event) -> None:
        msg = self._serialize(event)
        if msg is not None and msg["type"] == "status":
            h = self.session.device_health()     # already updated for this status (S43)
            msg.update(since=h["since"], since_epoch=h["since_epoch"], reconnects=h["reconnects"],
                       error=h["error"])
        skip = None
        if msg is None and self.echo_tx and event.kind == EventKind.LINE \
                and event.direction == Direction.TX:
            msg = {
                "type": "tx_echo",
                "seq": event.seq,
                "wall": event.stamp.wall_str(),
                "elapsed": round(event.stamp.elapsed, 4),
                "text": event.text,
            }
            skip = getattr(self._origin, "client", None)
        if msg is None:
            return
        line = encode_message(msg)
        with self._clients_lock:
            clients = [c for c in self._clients if c is not skip]
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
            return {"type": "status", "state": event.text, "meta": event.meta}  # since/reconnects added in _on_event
        return None

    def _send_now(self, client: _Client, msg: dict) -> None:
        try:
            client.conn.sendall(encode_message(msg))
        except OSError:
            pass
