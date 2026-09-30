"""S38: a proxy client's window size reaches a device that can use it."""

from __future__ import annotations

import time

import pytest

from uart_proxy.core.session import UartSession
from uart_proxy.io.socket_source import SocketSource
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class SizedDevice(FakeSource):
    """A device that, like ssh:// or telnet://, can be told a window size."""

    def __init__(self):
        super().__init__()
        self.sizes = []

    def set_window_size(self, cols, rows):
        self.sizes.append((cols, rows))


@pytest.fixture
def served():
    made = []

    def make(device):
        session = UartSession(device)
        server = ProxyServer(session, {"rw": Role.FULL, "ro": Role.READONLY},
                             host="127.0.0.1", port=0)
        server.start()
        session.start()
        made.append((session, server))
        return server

    yield make
    for session, server in made:
        server.stop()
        session.stop()


def _client(server, code="rw"):
    client = SocketSource("127.0.0.1", server.port, code)
    return client


def test_a_full_client_resizes_the_device(served):
    device = SizedDevice()
    server = served(device)
    client = _client(server)
    client.open()
    try:
        client.set_window_size(132, 43)
        assert _wait_for(lambda: device.sizes == [(132, 43)])
    finally:
        client.close()


def test_a_size_set_before_connecting_is_sent_once_connected(served):
    device = SizedDevice()
    server = served(device)
    client = _client(server)
    client.set_window_size(100, 30)       # the TUI sizes itself before the socket is up
    client.open()
    try:
        assert _wait_for(lambda: device.sizes == [(100, 30)])
    finally:
        client.close()


def test_the_size_is_sent_again_after_a_reconnect(served):
    device = SizedDevice()
    server = served(device)
    client = _client(server)
    client.set_window_size(90, 25)
    client.open()
    assert _wait_for(lambda: device.sizes == [(90, 25)])
    client.close()
    client.open()
    try:
        assert _wait_for(lambda: device.sizes == [(90, 25), (90, 25)])
    finally:
        client.close()


def test_a_readonly_viewer_cannot_reshape_the_device(served):
    device = SizedDevice()
    server = served(device)
    viewer = _client(server, "ro")
    viewer.open()
    try:
        viewer.set_window_size(200, 60)
        time.sleep(0.3)
        assert device.sizes == []
    finally:
        viewer.close()


def test_the_server_ignores_a_readonly_resize_even_if_one_is_sent(served):
    """Defence on the server, not just politeness in our client."""
    import socket

    from uart_proxy.proxy.protocol import encode_message

    device = SizedDevice()
    server = served(device)
    with socket.create_connection(("127.0.0.1", server.port), timeout=3) as s:
        s.sendall(encode_message({"type": "auth", "code": "ro"}))
        s.recv(4096)
        s.sendall(encode_message({"type": "resize", "cols": 200, "rows": 60}))
        time.sleep(0.3)
    assert device.sizes == []


@pytest.mark.parametrize("msg", [
    {"type": "resize", "cols": "wide", "rows": 30},
    {"type": "resize", "cols": 0, "rows": 30},
    {"type": "resize", "cols": 5000, "rows": 30},
    {"type": "resize"},
])
def test_nonsense_sizes_are_ignored(served, msg):
    import socket

    from uart_proxy.proxy.protocol import encode_message

    device = SizedDevice()
    server = served(device)
    with socket.create_connection(("127.0.0.1", server.port), timeout=3) as s:
        s.sendall(encode_message({"type": "auth", "code": "rw"}))
        s.recv(4096)
        s.sendall(encode_message(msg))
        time.sleep(0.2)
    assert device.sizes == []


def test_a_device_that_cannot_use_a_size_is_unaffected(served):
    device = FakeSource()                 # a UART: no window size to set
    device.feed(b"still here\n")
    server = served(device)
    client = _client(server)
    client.open()
    try:
        client.set_window_size(120, 40)
        time.sleep(0.2)
        assert server.client_count == 1, "the client was not dropped"
    finally:
        client.close()


def test_the_whole_chain_to_a_telnet_server(served):
    """remote client → proxy → telnet:// source → NAWS on the far server."""
    from test_telnet_source import IAC, NAWS, SB, SE, TelnetServer, b

    from uart_proxy.io.telnet_source import TelnetSource

    far = TelnetServer()
    try:
        source = TelnetSource(f"telnet://127.0.0.1:{far.port}")
        server = served(source)
        assert _wait_for(lambda: NAWS in source.protocol.us)
        client = _client(server)
        client.open()
        try:
            client.set_window_size(140, 45)
            assert _wait_for(lambda: bytes(far.received).endswith(
                b(IAC, SB, NAWS, 0, 140, 0, 45, IAC, SE)))
        finally:
            client.close()
    finally:
        far.close()
