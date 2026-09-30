"""S34: a serial port on the network — socket:// and rfc2217://."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import threading
import time

import pytest

from uart_proxy import cli
from uart_proxy.core.events import Direction, EventKind
from uart_proxy.core.pty_proxy import device_stem
from uart_proxy.core.session import UartSession
from uart_proxy.io.url_source import UrlSource, check_port_url, is_port_url


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── URLs ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("port, expected", [
    ("socket://10.0.0.5:4001", True), ("rfc2217://lab:7001", True),
    ("/dev/tty.usbserial-110", False), ("COM3", False), ("", False), (None, False),
])
def test_a_url_is_told_apart_from_a_device_path(port, expected):
    assert is_port_url(port) is expected


def test_the_two_schemes_are_accepted():
    assert check_port_url("socket://10.0.0.5:4001") is None
    assert check_port_url("rfc2217://lab.local:7001") is None


@pytest.mark.parametrize("url, says", [
    ("ftp://lab:21", "unsupported"),
    ("loop://", "unsupported"),
    ("socket://lab", "needs a host and a port"),
    ("socket://:4001", "needs a host and a port"),
    ("socket://lab:notaport", "bad port"),
])
def test_anything_else_is_refused_with_what_would_work(url, says):
    message = check_port_url(url)
    assert message is not None and says in message
    if says == "unsupported":
        assert "socket://" in message and "rfc2217://" in message


def test_a_url_port_gets_a_file_name_safe_stem():
    assert device_stem("socket://10.0.0.5:4001") == "socket-10.0.0.5-4001"
    assert device_stem("rfc2217://lab:7001") == "rfc2217-lab-7001"
    assert "/" not in device_stem("socket://[::1]:4001")


def test_the_description_says_whose_settings_apply():
    raw = UrlSource("socket://lab:4001")
    assert "raw TCP" in raw.description() and "server's" in raw.description()
    assert not raw.applies_settings
    remote = UrlSource("rfc2217://lab:7001")
    assert remote.applies_settings and "@ 115200 8N1" in remote.description()


# ── a raw TCP "device" ──────────────────────────────────────────────────────


class TcpDevice:
    """A console server port: greets, then echoes, one client at a time."""

    def __init__(self, port: int = 0, greeting: bytes = b"login: ") -> None:
        self.greeting = greeting
        self.received = bytearray()
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", port))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.conn = None
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self.conn = conn
            conn.sendall(self.greeting)
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                self.received.extend(data)
            self.conn = None

    def send(self, data: bytes) -> None:
        assert _wait_for(lambda: self.conn is not None)
        self.conn.sendall(data)

    def drop_client(self) -> None:
        if self.conn is not None:
            self.conn.shutdown(socket.SHUT_RDWR)
            self.conn.close()

    def close(self) -> None:
        self._stop = True
        self.drop_client()
        self._srv.close()


@pytest.fixture
def tcp_device():
    made = []

    def make(**kw):
        d = TcpDevice(**kw)
        made.append(d)
        return d

    yield make
    for d in made:
        d.close()


def _session(url):
    session = UartSession(UrlSource(url), default_eol=b"\r", reconnect_interval=0.05)
    events = []
    session.bus.subscribe(events.append)
    return session, events


def _rx_text(events) -> str:
    return "".join(e.text for e in events
                   if e.kind is EventKind.DATA and e.direction is Direction.RX)


def test_raw_tcp_carries_both_directions(tcp_device):
    device = tcp_device()
    session, events = _session(f"socket://127.0.0.1:{device.port}")
    session.start()
    try:
        assert _wait_for(lambda: "login: " in _rx_text(events)), "RX from the server"
        session.send_text("root")
        assert _wait_for(lambda: bytes(device.received) == b"root\r"), "TX to the server"
    finally:
        session.stop()


def test_a_server_that_is_not_up_yet_is_waited_for(tcp_device):
    port = _free_port()
    session, events = _session(f"socket://127.0.0.1:{port}")
    session.start()
    try:
        assert _wait_for(lambda: any(e.kind is EventKind.STATUS and e.text == "waiting"
                                     for e in events), timeout=10)
        try:
            tcp_device(port=port)
        except OSError:
            pytest.skip("another process took the port in between")
        assert _wait_for(lambda: session.is_connected and "login: " in _rx_text(events),
                         timeout=15)
    finally:
        session.stop()


def test_a_dropped_connection_reconnects(tcp_device):
    """A console server rebooting, or ser2net restarted: S12 reconnects."""
    device = tcp_device(greeting=b"hello ")
    session, events = _session(f"socket://127.0.0.1:{device.port}")
    session.start()
    try:
        assert _wait_for(lambda: _rx_text(events).count("hello ") == 1)
        device.drop_client()
        assert _wait_for(lambda: _rx_text(events).count("hello ") == 2, timeout=8), \
            "connected again, and greeted again"
        statuses = [e.text for e in events if e.kind is EventKind.STATUS]
        assert "reconnecting" in statuses
    finally:
        session.stop()


# ── a real RFC 2217 server (pyserial's own PortManager over loop://) ────────


class Rfc2217Server:
    """pyserial's reference RFC 2217 server, fronting a loop:// port, so what
    the client writes comes back — and the client's settings land on it."""

    def __init__(self) -> None:
        import serial
        import serial.rfc2217

        self.port_obj = serial.serial_for_url("loop://", timeout=0.05)
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._rfc = serial.rfc2217
        self._alive = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        lock = threading.Lock()

        class Writer:
            @staticmethod
            def write(data):
                with lock:
                    conn.sendall(data)

        manager = self._rfc.PortManager(self.port_obj, Writer())

        def serial_to_net():
            while self._alive:
                data = self.port_obj.read(self.port_obj.in_waiting or 1)
                if data:
                    Writer.write(b"".join(manager.escape(data)))

        threading.Thread(target=serial_to_net, daemon=True).start()
        while self._alive:
            try:
                data = conn.recv(1024)
            except OSError:
                break
            if not data:
                break
            self.port_obj.write(b"".join(manager.filter(data)))
        conn.close()

    def close(self) -> None:
        self._alive = False
        self._srv.close()


@pytest.fixture
def rfc2217_server():
    server = Rfc2217Server()
    yield server
    server.close()


def test_rfc2217_applies_our_settings_to_the_far_port(rfc2217_server):
    from uart_helper import UARTConfig

    source = UrlSource(f"rfc2217://127.0.0.1:{rfc2217_server.port}",
                       UARTConfig(baudrate=57600, parity="E"))
    source.open()
    try:
        assert _wait_for(lambda: rfc2217_server.port_obj.baudrate == 57600)
        assert rfc2217_server.port_obj.parity == "E"
    finally:
        source.close()


def test_rfc2217_carries_data_including_the_telnet_escape_byte(rfc2217_server):
    """0xFF is Telnet's IAC; it must be escaped on the way and unescaped back."""
    session = UartSession(UrlSource(f"rfc2217://127.0.0.1:{rfc2217_server.port}"),
                          reconnect_interval=0.05)
    got = bytearray()
    session.bus.subscribe(lambda e: got.extend(e.data)
                          if e.kind is EventKind.DATA and e.direction is Direction.RX
                          else None)
    session.start()
    try:
        assert _wait_for(lambda: session.is_connected)
        payload = b"bin\xff\x00\xfftail"
        session.write(payload)
        assert _wait_for(lambda: payload in bytes(got)), f"got {bytes(got)!r}"
    finally:
        session.stop()


def test_reading_does_not_resend_the_settings_every_time(rfc2217_server, monkeypatch):
    """Setting pyserial's timeout reconfigures an open port — for rfc2217 a
    round trip to the server. It must happen once, not on every read."""
    source = UrlSource(f"rfc2217://127.0.0.1:{rfc2217_server.port}")
    source.open()
    try:
        calls = []
        real = source._serial._reconfigure_port
        monkeypatch.setattr(source._serial, "_reconfigure_port",
                            lambda *a, **k: calls.append(1) or real(*a, **k))
        for _ in range(5):
            source.read(64, 0.05)
        assert len(calls) <= 1
    finally:
        source.close()


# ── through the CLI ─────────────────────────────────────────────────────────


def test_connect_refuses_an_unsupported_url_before_doing_anything(capsys):
    assert cli.main(["connect", "--port", "ftp://lab:21", "--no-log"]) == 1
    assert "unsupported port URL" in capsys.readouterr().err


@pytest.mark.skipif(os.name != "posix", reason="pty mirrors")
def test_connect_to_a_network_port_end_to_end(tcp_device, tmp_path):
    device = tcp_device(greeting=b"U-Boot 2024.01\r\n")
    mirrors = tmp_path / "mirrors"
    url = f"socket://127.0.0.1:{device.port}"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port", url, "--no-tui",
         "--output-dir", str(tmp_path / "logs"), "--proxy-dir", str(mirrors),
         "--proxy-count", "1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, HOME=str(tmp_path)))
    try:
        link = mirrors / f"socket-127.0.0.1-{device.port}-0"
        assert _wait_for(link.exists, 15), "mirror named after the URL"
        raw = tmp_path / "logs" / "output.log"
        assert _wait_for(lambda: raw.exists() and b"U-Boot" in raw.read_bytes(), 10)
    finally:
        proc.terminate()
        out, err = proc.communicate(timeout=10)
    out = out.decode()
    assert "network port (raw TCP" in out and "no exclusive claim" in out
    assert "COULD NOT claim" not in out, "no local-port exclusivity story"
    header = (tmp_path / "logs" / "output-timestamp.log").read_text().splitlines()[0]
    assert url in header


def test_what_the_server_says_on_connect_is_never_flushed_away(tcp_device, monkeypatch):
    """pyserial's open() ends by flushing input; a banner sent the moment we
    connect used to be discarded whenever it beat that flush (seen under load).
    Force the race: make opening slow enough that the banner is always first."""
    import serial.urlhandler.protocol_socket as sock

    real = sock.Serial._reconfigure_port

    def slow(self, *a, **k):
        time.sleep(0.3)   # the server's greeting lands in this gap
        return real(self, *a, **k)

    monkeypatch.setattr(sock.Serial, "_reconfigure_port", slow)
    device = tcp_device(greeting=b"banner-sent-on-connect\r\n")
    source = UrlSource(f"socket://127.0.0.1:{device.port}")
    source.open()
    try:
        got = b""
        deadline = time.monotonic() + 5
        while b"banner-sent-on-connect" not in got and time.monotonic() < deadline:
            got += source.read(4096, 0.1)
        assert b"banner-sent-on-connect" in got, "the first thing the device said was lost"
        # …and flushing still works afterwards: only open() is spared.
        assert type(source._serial).reset_input_buffer is not None
        assert "reset_input_buffer" not in vars(source._serial)
    finally:
        source.close()
