"""
Interop: the reference uart_helper broker (examples/uart_helper_broker.py)
speaks the same protocol as uart-proxy, so uart-proxy's UNMODIFIED client
attaches over loopback TCP. Proves the integration story (Windows + macOS via
loopback TCP) end to end.

Uses a pty as a fake UART, so it's POSIX-only (skipped on Windows, which has no
pty; the broker itself is cross-platform).
"""

from __future__ import annotations

import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")

# Make the examples/ broker importable.
_EXAMPLES = os.path.join(os.path.dirname(os.path.dirname(__file__)), "examples")
sys.path.insert(0, _EXAMPLES)


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _make_broker():
    import pty

    from uart_helper import PortIdentity, UARTConfig, UARTDevice
    from uart_helper_broker import UartHelperBroker

    master, slave = pty.openpty()
    os.set_blocking(master, False)  # so test reads never block
    dev = UARTDevice(PortIdentity(device=os.ttyname(slave)), UARTConfig(baudrate=115200))
    broker = UartHelperBroker(
        dev, host="127.0.0.1", port=0, auth={"123456": "full", "000000": "readonly"}
    )
    broker.start()
    return master, broker


def test_uart_proxy_client_attaches_to_broker():
    from uart_proxy.io.socket_source import SocketSource
    from uart_proxy.proxy.protocol import Role

    master, broker = _make_broker()
    try:
        client = SocketSource("127.0.0.1", broker.port, "123456")
        client.open()
        assert client.role == Role.FULL

        os.write(master, b"hello\r\n")
        got = bytearray()

        def pump():
            got.extend(client.read(4096, 0.1))
            return b"hello" in bytes(got)

        assert _wait_for(pump)

        # A full client may write; it reaches the real device (pty master).
        client.write(b"cmd\r")

        def got_cmd():
            try:
                return os.read(master, 1024) == b"cmd\r"
            except BlockingIOError:
                return False

        assert _wait_for(got_cmd)
        client.close()
    finally:
        broker.stop()


def test_readonly_client_cannot_write_to_broker():
    from uart_proxy.io.socket_source import SocketSource, SocketSourceError
    from uart_proxy.proxy.protocol import Role

    master, broker = _make_broker()
    try:
        ro = SocketSource("127.0.0.1", broker.port, "000000")
        ro.open()
        assert ro.role == Role.READONLY
        with pytest.raises(SocketSourceError):
            ro.write(b"x")
        ro.close()
    finally:
        broker.stop()


def test_embedded_tee_mode():
    # The integration scenario: the app owns the UARTDevice and tees data into
    # the broker via publish_rx(); client tx is delivered to the app via on_tx.
    from uart_proxy.io.socket_source import SocketSource
    from uart_proxy.proxy.protocol import Role
    from uart_helper_broker import UartHelperBroker

    sent_by_clients: list[bytes] = []
    broker = UartHelperBroker(
        host="127.0.0.1", port=0, auth={"123456": "full"},
        on_tx=sent_by_clients.append, source="my-app COM3",
    )
    broker.start()  # no device — embedded mode
    try:
        client = SocketSource("127.0.0.1", broker.port, "123456")
        client.open()
        assert client.role == Role.FULL
        assert "my-app COM3" in client.remote_source_desc

        # App tees device output -> client receives it. (A broker only forwards
        # to currently-connected clients, like tail -f, so stream it as a real
        # device would rather than relying on a single pre-registration shot.)
        got = bytearray()

        def pump():
            broker.publish_rx(b"telemetry-42\r\n")
            got.extend(client.read(4096, 0.1))
            return b"telemetry-42" in bytes(got)

        assert _wait_for(pump)

        # Client command -> delivered to the app's on_tx callback.
        client.write(b"reboot\r")
        assert _wait_for(lambda: b"reboot\r" in b"".join(sent_by_clients))
        client.close()
    finally:
        broker.stop()


def test_bad_code_rejected_by_broker():
    from uart_proxy.io.socket_source import SocketSource, SocketSourceError

    master, broker = _make_broker()
    try:
        bad = SocketSource("127.0.0.1", broker.port, "nope")
        with pytest.raises(SocketSourceError):
            bad.open()
    finally:
        broker.stop()
