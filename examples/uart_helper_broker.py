"""
Reference broker — expose a uart_helper-owned serial port over loopback TCP.

Use this when an integration app already owns the serial port via uart_helper
(so uart-proxy can't open it — UART is exclusive). The broker opens the port,
fans the traffic out to multiple TCP clients, and lets full-role clients write
back. uart-proxy then attaches with NO changes:

    uart-proxy remote --host 127.0.0.1 --port 9600 --auth 123456

Why loopback TCP (not a Unix socket file): it is identical on Windows and
macOS. CPython does not expose AF_UNIX on Windows, so socket files aren't
portable; 127.0.0.1 is.

This file is self-contained (stdlib + uart_helper only) so it can be dropped
into uart_helper as ``uart_helper/broker.py`` unchanged. It speaks exactly the
JSON-lines protocol uart-proxy uses — see ../PROTOCOL.md.

Run standalone:

    python uart_helper_broker.py --port COM3 --baud 115200 \
        --auth 123456 --auth 000000:readonly
"""

from __future__ import annotations

import argparse
import itertools
import json
import queue
import socket
import threading
import time
from datetime import datetime, timedelta

from uart_helper import PortIdentity, UARTConfig, UARTDevice

# ── protocol (matches uart-proxy; keep in sync with PROTOCOL.md) ─────────────

EOL = {"crlf": b"\r\n", "lf": b"\n", "cr": b"\r", "none": b""}
_AUTH_TIMEOUT = 10.0
_SEND_QUEUE_MAX = 10000


def _encode(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _decode(line: bytes) -> dict | None:
    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def parse_auth_spec(spec: str) -> tuple[str, str]:
    """'CODE' -> (CODE, 'full'); 'CODE:readonly' -> (CODE, 'readonly')."""
    if ":" in spec:
        code, _, role = spec.partition(":")
        role = role.strip().lower()
        if role not in ("full", "readonly"):
            raise ValueError(f"unknown role {role!r}")
        return code.strip(), role
    return spec.strip(), "full"


# ── client connection ────────────────────────────────────────────────────────


class _Client:
    def __init__(self, conn: socket.socket, addr) -> None:
        self.conn = conn
        self.addr = addr
        self.role: str | None = None
        self.q: "queue.Queue[bytes | None]" = queue.Queue(maxsize=_SEND_QUEUE_MAX)
        self._buf = bytearray()
        self._writer: threading.Thread | None = None

    def enqueue(self, line: bytes) -> bool:
        try:
            self.q.put_nowait(line)
            return True
        except queue.Full:
            return False

    def start_writer(self) -> None:
        self._writer = threading.Thread(target=self._write_loop, daemon=True)
        self._writer.start()

    def _write_loop(self) -> None:
        while True:
            line = self.q.get()
            if line is None:
                break
            try:
                self.conn.sendall(line)
            except OSError:
                break

    def shutdown(self) -> None:
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.conn.close()
        except OSError:
            pass

    def read_lines(self):
        while True:
            idx = self._buf.find(b"\n")
            if idx >= 0:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 1]
                yield line
                continue
            try:
                chunk = self.conn.recv(65536)
            except (OSError, socket.timeout):
                return
            if not chunk:
                return
            self._buf.extend(chunk)


# ── broker ────────────────────────────────────────────────────────────────────


class UartHelperBroker:
    def __init__(
        self,
        device: UARTDevice | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 9600,
        auth: dict[str, str] | None = None,
        on_tx=None,
        source: str = "",
    ) -> None:
        """
        Two ways to use it:

        * **Owned mode** — pass ``device``; the broker opens it, runs the read
          loop, and writes client ``tx`` to it. Good for a standalone bridge.

        * **Embedded/tee mode** — pass ``on_tx`` (and usually no ``device``).
          YOUR app keeps owning the UART. Feed received bytes in with
          ``publish_rx(data)``, and the broker calls ``on_tx(data)`` when a
          full-role client sends a command. This avoids two readers on one
          port — the right fit when the app both uses the data and exposes it.

              dev = UARTDevice(...); dev.open()
              broker = UartHelperBroker(host="127.0.0.1", port=9600,
                                        auth={"123456": "full"},
                                        on_tx=lambda b: dev.write(b),
                                        source="my-app COM3")
              broker.start()
              while running:
                  data = dev.read(...).data
                  if data:
                      my_app_consume(data)
                      broker.publish_rx(data)   # tee to uart-proxy
        """
        self.device = device
        self.host = host
        self.port = port
        self.auth = auth or {"123456": "full"}
        self._on_tx = on_tx
        self._source = source

        self._srv: socket.socket | None = None
        self._stop = threading.Event()
        self._clients: set[_Client] = set()
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._seq = itertools.count(1)
        self._start_wall = datetime.now()
        self._start_mono = time.monotonic()

    # lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((self.host, self.port))
        self.port = srv.getsockname()[1]  # supports port=0
        srv.listen(16)
        srv.settimeout(0.5)
        self._srv = srv
        threading.Thread(target=self._accept_loop, daemon=True).start()
        # Owned mode: the broker opens and reads the device itself. In
        # embedded/tee mode there is no device here — the app feeds publish_rx.
        if self.device is not None:
            self.device.open()
            threading.Thread(target=self._read_loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients)
            self._clients.clear()
        for c in clients:
            c.shutdown()
        if self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass

    # serial -> clients -------------------------------------------------------

    def publish_rx(self, data: bytes) -> None:
        """Push received device bytes to all clients (embedded/tee mode)."""
        if not data:
            return
        elapsed = time.monotonic() - self._start_mono
        wall = self._start_wall + timedelta(seconds=elapsed)
        self._broadcast({
            "type": "rx",
            "seq": next(self._seq),
            "wall": wall.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(elapsed, 4),
            "hex": data.hex(),
            "text": data.decode("utf-8", errors="replace"),
        })

    def _read_loop(self) -> None:
        while not self._stop.is_set():
            try:
                waiting = self.device.in_waiting
                if waiting:
                    data = self.device.read(waiting).data
                else:
                    data = self.device.read(1, timeout_ms=100).data
            except Exception as exc:  # device dropped, etc.
                self._broadcast({"type": "status", "state": "error",
                                 "meta": {"error": str(exc)}})
                break
            self.publish_rx(data)

    def _broadcast(self, msg: dict) -> None:
        line = _encode(msg)
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            if not c.enqueue(line):
                c.shutdown()
                with self._lock:
                    self._clients.discard(c)

    # clients -> serial -------------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._srv is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn, addr), daemon=True).start()

    def _handle(self, conn: socket.socket, addr) -> None:
        client = _Client(conn, addr)
        try:
            conn.settimeout(_AUTH_TIMEOUT)
            if not self._auth(client):
                return
            conn.settimeout(None)
            with self._lock:
                self._clients.add(client)
            client.start_writer()
            for line in client.read_lines():
                msg = _decode(line)
                if not msg:
                    continue
                if msg.get("type") == "tx":
                    self._handle_tx(client, msg)
                elif msg.get("type") == "ping":
                    client.enqueue(_encode({"type": "pong"}))
        finally:
            with self._lock:
                self._clients.discard(client)
            client.shutdown()

    def _auth(self, client: _Client) -> bool:
        for line in client.read_lines():
            msg = _decode(line)
            if not msg or msg.get("type") != "auth":
                self._send_now(client, {"type": "auth_fail", "reason": "expected auth"})
                return False
            role = self.auth.get(str(msg.get("code", "")))
            if role is None:
                self._send_now(client, {"type": "auth_fail", "reason": "invalid code"})
                return False
            client.role = role
            if self._source:
                source = self._source
            elif self.device is not None:
                source = f"{self.device.identity.device} @ {self.device.config.baudrate}"
            else:
                source = "uart_helper"
            self._send_now(client, {"type": "auth_ok", "role": role, "source": source})
            return True
        return False

    def _handle_tx(self, client: _Client, msg: dict) -> None:
        if client.role != "full":
            client.enqueue(_encode({"type": "notice", "text": "write denied (read-only)"}))
            return
        if "hex" in msg:
            try:
                data = bytes.fromhex(msg["hex"])
            except ValueError:
                return
        elif "text" in msg:
            data = str(msg["text"]).encode("utf-8", "replace") + EOL.get(str(msg.get("eol", "cr")), b"\r")
        else:
            return
        with self._write_lock:
            if self._on_tx is not None:      # embedded/tee mode
                self._on_tx(data)
            elif self.device is not None:    # owned mode
                self.device.write(data)

    def _send_now(self, client: _Client, msg: dict) -> None:
        try:
            client.conn.sendall(_encode(msg))
        except OSError:
            pass


# ── standalone CLI ────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Expose a uart_helper port over loopback TCP for uart-proxy.")
    ap.add_argument("--port", required=True, help="Serial device path / COM port.")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--listen", default="127.0.0.1", help="Bind address (default 127.0.0.1).")
    ap.add_argument("--listen-port", type=int, default=9600)
    ap.add_argument("--auth", action="append", metavar="CODE[:role]",
                    help="Auth code, optional role full|readonly. Repeatable.")
    args = ap.parse_args()

    auth: dict[str, str] = {}
    for spec in args.auth or ["123456"]:
        code, role = parse_auth_spec(spec)
        auth[code] = role

    dev = UARTDevice(PortIdentity(device=args.port), UARTConfig(baudrate=args.baud))
    broker = UartHelperBroker(dev, host=args.listen, port=args.listen_port, auth=auth)
    broker.start()
    print(f"Broker on {args.listen}:{broker.port} — attach with: "
          f"uart-proxy remote --host {args.listen} --port {broker.port} --auth <code>")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        broker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
