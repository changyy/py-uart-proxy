"""S43, S45: device health on the wire and in the client; when clients were last heard."""

from __future__ import annotations

import socket
import threading
import time

import pytest

import uart_proxy.health as health
from uart_proxy.client import SessionClient
from uart_proxy.core.session import UartSession
from uart_proxy.health import assess
from uart_proxy.proxy.protocol import Role, decode_message, encode_message
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# ── assess ──────────────────────────────────────────────────────────────────


def _device(state="connected", **kw):
    return {"state": state, "since": "2026-10-04 09:30:00", "since_age": kw.pop("since_age", 300.0),
            "error": kw.pop("error", None), "reconnects": kw.pop("reconnects", 0),
            "silent_for": kw.pop("silent_for", 1.0)}


def test_s43_settled_and_connected_is_ok():
    verdict = assess(shared=True, device=_device())
    assert verdict["level"] == "ok" and verdict["advice"] == ""


@pytest.mark.parametrize("state", ["waiting", "error", "reconnecting", "disconnected"])
def test_s43_a_device_that_is_not_there_is_down_with_advice(state):
    verdict = assess(shared=True, device=_device(state, error="device not present"))
    assert verdict["level"] == "down"
    assert "re-plug" in verdict["advice"] or "connect it again" in verdict["advice"]


def test_s43_absent_names_the_error():
    verdict = assess(shared=True, device=_device("waiting", error="[Errno 2] No such file"))
    assert "No such file" in verdict["summary"] and "cable" in verdict["advice"]


def test_s43_not_shared_is_down_and_says_share_again():
    verdict = assess(shared=False, device=None, link_error="the session closed the connection")
    assert verdict["level"] == "down" and "share" in verdict["advice"].lower()


def test_s43_connecting_and_a_fresh_reconnect_are_degraded():
    assert assess(shared=True, device=_device("connecting"))["level"] == "degraded"
    fresh = assess(shared=True, device=_device(reconnects=1, since_age=5.0))
    assert fresh["level"] == "degraded" and "missing" in fresh["advice"]
    assert assess(shared=True, device=_device(reconnects=1, since_age=61.0))["level"] == "ok"


def test_s43_silence_is_named_past_a_minute_only():
    assert assess(shared=True, device=_device(silent_for=30.0))["advice"] == ""
    quiet = assess(shared=True, device=_device(silent_for=95.0))
    assert quiet["level"] == "ok" and "95 s" in quiet["advice"] and "reset" in quiet["advice"]


# ── the session and the wire ────────────────────────────────────────────────


def _served(device):
    session = UartSession(device, reconnect_interval=0.05)
    server = ProxyServer(session, {"rw": Role.FULL, "ro": Role.READONLY}, host="127.0.0.1", port=0)
    server.start()
    session.start()
    return session, server


def test_s43_the_session_keeps_its_device_health():
    device = FakeSource()
    session, server = _served(device)
    try:
        assert _wait_for(lambda: session.device_health()["state"] == "connected")
        h = session.device_health()
        assert h["reconnects"] == 0 and h["since"] and h["error"] is None
        device.feed(b"hi\n")
        assert _wait_for(lambda: session.device_health()["last_output"] is not None)
        device.drop()
        assert _wait_for(lambda: session.device_health()["reconnects"] == 1)
        assert session.device_health()["state"] == "connected"
    finally:
        server.stop()
        session.stop()


def test_s43_a_client_attaching_during_a_drop_knows_at_once():
    device = FakeSource(fail_opens=10_000)          # absent, retried
    session, server = _served(device)
    try:
        assert _wait_for(lambda: session.device_health()["state"] == "waiting")
        client = SessionClient("127.0.0.1", server.port, "ro", replay=0)
        client.connect()
        try:
            assert client.device["state"] == "waiting", "from auth_ok, before any change"
            assert "not present" in (client.device["error"] or "")
            assert client.health()["level"] == "down"
            device.fail_opens = 0                     # plugged back in
            assert _wait_for(lambda: client.device["state"] == "connected")
            assert client.health()["level"] == "ok", "a first connection is not a reconnect"
        finally:
            client.close()
    finally:
        server.stop()
        session.stop()


def test_s43_a_drop_and_return_is_degraded_until_it_settles(monkeypatch):
    monkeypatch.setattr(health, "SETTLE_SECONDS", 0.5)
    device = FakeSource()
    session, server = _served(device)
    try:
        assert _wait_for(lambda: session.device_health()["state"] == "connected")
        client = SessionClient("127.0.0.1", server.port, "ro", replay=0)
        client.connect()
        try:
            seen = []
            client.on_device = lambda d: seen.append(d["state"])
            device.drop()
            assert _wait_for(lambda: client.device["reconnects"] == 1 and client.device["state"] == "connected")
            assert {"error", "reconnecting"} & set(seen)
            assert client.health()["level"] == "degraded"
            assert _wait_for(lambda: client.health()["level"] == "ok", 3)
        finally:
            client.close()
    finally:
        server.stop()
        session.stop()


def test_s43_a_server_that_goes_silent_is_a_lost_link():
    """A server that authenticates, then never answers: a half-open link."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    held = []

    def serve():
        conn, _ = srv.accept()
        held.append(conn)
        conn.recv(4096)
        conn.sendall(encode_message({"type": "auth_ok", "role": "readonly", "source": "x"}))

    threading.Thread(target=serve, daemon=True).start()
    client = SessionClient("127.0.0.1", port, "ro", replay=0, heartbeat=0.2)
    client.connect()
    try:
        assert _wait_for(lambda: not client.connected, 3), "three silent heartbeats"
        verdict = client.health()
        assert verdict["level"] == "down" and "share" in verdict["advice"].lower()
    finally:
        client.close()
        srv.close()
        for c in held:
            c.close()


# ── S45 ─────────────────────────────────────────────────────────────────────


def test_s45_last_seen_follows_a_clients_messages():
    device = FakeSource()
    session, server = _served(device)
    try:
        sock = socket.create_connection(("127.0.0.1", server.port))
        sock.sendall(encode_message({"type": "auth", "code": "ro", "client": "agent"}))
        assert decode_message(sock.recv(4096).split(b"\n")[0])["type"] == "auth_ok"
        assert _wait_for(lambda: server.clients())
        first = server.clients()[0]["last_seen"]
        time.sleep(0.3)
        assert server.clients()[0]["last_seen"] == first, "silent: it stays put"
        sock.sendall(encode_message({"type": "ping"}))
        assert _wait_for(lambda: server.clients()[0]["last_seen"] > first)
        sock.close()
    finally:
        server.stop()
        session.stop()
