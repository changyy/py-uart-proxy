"""S6: end-to-end proxy — server + SocketSource client over a real socket."""

from __future__ import annotations

import time

import pytest

from uart_proxy.core.session import UartSession
from uart_proxy.io.socket_source import SocketSource, SocketSourceError
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _make_server(auth):
    device = FakeSource(echo=False)
    session = UartSession(device)
    server = ProxyServer(session, auth, host="127.0.0.1", port=0)
    server.start()      # binds; server.port now holds the ephemeral port
    session.start()
    return device, session, server


def test_full_client_receives_rx_and_can_write():
    device, session, server = _make_server({"123456": Role.FULL})
    try:
        client = SocketSource("127.0.0.1", server.port, "123456")
        client.open()
        assert client.role == Role.FULL

        # Device output is forwarded to the client and reconstructed as bytes.
        device.feed(b"hello\n")
        received = bytearray()

        def pump():
            received.extend(client.read(4096, timeout=0.2))
            return b"hello\n" in bytes(received)

        assert _wait_for(pump)

        # A full client may write; it reaches the real device.
        client.write(b"AT\r\n")
        assert _wait_for(lambda: b"AT\r\n" in b"".join(device.writes))

        client.close()
    finally:
        server.stop()
        session.stop()


def test_readonly_client_cannot_write():
    device, session, server = _make_server({"000000": Role.READONLY})
    try:
        client = SocketSource("127.0.0.1", server.port, "000000")
        client.open()
        assert client.role == Role.READONLY
        assert client.writable is False

        with pytest.raises(SocketSourceError):
            client.write(b"AT\r\n")
        # nothing reached the device
        time.sleep(0.2)
        assert device.writes == []
        client.close()
    finally:
        server.stop()
        session.stop()


def test_read_timeout_returns_empty_not_eof():
    # Regression: a recv timeout (no data yet) must return b"", not be mistaken
    # for an orderly peer shutdown (which raises).
    device, session, server = _make_server({"123456": Role.FULL})
    try:
        client = SocketSource("127.0.0.1", server.port, "123456")
        client.open()
        for _ in range(3):
            assert client.read(4096, timeout=0.1) == b""  # idle device, no EOF
        client.close()
    finally:
        server.stop()
        session.stop()


def test_bad_auth_code_is_rejected():
    device, session, server = _make_server({"123456": Role.FULL})
    try:
        client = SocketSource("127.0.0.1", server.port, "wrong-code")
        with pytest.raises(SocketSourceError):
            client.open()
    finally:
        server.stop()
        session.stop()
