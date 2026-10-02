"""S40: who sent it — TX origin, client names, the connection list."""

from __future__ import annotations

import socket
import time

from uart_proxy.core.events import Direction, EventKind
from uart_proxy.core.session import UartSession
from uart_proxy.proxy.protocol import Role, decode_message, encode_message
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _served():
    device = FakeSource()
    session = UartSession(device)
    server = ProxyServer(session, {"rw": Role.FULL, "ro": Role.READONLY}, host="127.0.0.1", port=0)
    server.start()
    session.start()
    assert _wait_for(lambda: session.is_connected)
    return device, session, server


def _client(server, code, name=None):
    sock = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    hello = {"type": "auth", "code": code}
    if name is not None:
        hello["client"] = name
    sock.sendall(encode_message(hello))
    reply = b""
    while not reply.endswith(b"\n"):
        reply += sock.recv(4096)
    assert decode_message(reply)["type"] == "auth_ok"
    return sock


def test_s40_a_proxy_clients_tx_names_its_origin():
    device, session, server = _served()
    seen = []
    session.bus.subscribe(lambda e: seen.append(e) if e.direction == Direction.TX else None)
    try:
        sock = _client(server, "rw", "uart-proxy mcp (claude-ai)")
        sock.sendall(encode_message({"type": "tx", "text": "reboot", "eol": "cr"}))
        assert _wait_for(lambda: any(e.kind == EventKind.LINE for e in seen))
        data = next(e for e in seen if e.kind == EventKind.DATA)
        line = next(e for e in seen if e.kind == EventKind.LINE)
        for event in (data, line):
            origin = event.meta["origin"]
            assert origin["via"] == "proxy" and origin["role"] == "full"
            assert origin["client"] == "uart-proxy mcp (claude-ai)"
            assert origin["address"].startswith("127.0.0.1:")
        sock.close()
    finally:
        server.stop()
        session.stop()


def test_s40_local_typing_has_no_origin():
    device, session, server = _served()
    seen = []
    session.bus.subscribe(lambda e: seen.append(e) if e.direction == Direction.TX else None)
    try:
        session.write(b"ls\r")
        assert _wait_for(lambda: len(seen) >= 2)
        assert all("origin" not in e.meta for e in seen)
        session.write(b"ls\r", origin={"via": "app"})
        assert _wait_for(lambda: any(e.meta.get("origin") == {"via": "app"} for e in seen))
    finally:
        server.stop()
        session.stop()


def test_s40_the_connection_list_names_each_client_and_forgets_it():
    device, session, server = _served()
    try:
        sock = _client(server, "ro", "x" * 100)
        assert _wait_for(lambda: len(server.clients()) == 1)
        (entry,) = server.clients()
        assert entry["role"] == "readonly" and entry["client"] == "x" * 64
        assert entry["address"].startswith("127.0.0.1:") and entry["connected_at"] <= time.time()
        unnamed = _client(server, "rw")
        assert _wait_for(lambda: len(server.clients()) == 2)
        assert {c["client"] for c in server.clients()} == {"x" * 64, ""}
        sock.close()
        unnamed.close()
        assert _wait_for(lambda: server.clients() == [])
    finally:
        server.stop()
        session.stop()
