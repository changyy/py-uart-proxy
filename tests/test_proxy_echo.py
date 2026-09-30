"""S29: proxy clients can see what was typed into the device — when asked for."""

from __future__ import annotations

import argparse
import time

import pytest

from uart_proxy import cli
from uart_proxy.cli import _maybe_build_proxy
from uart_proxy.core.events import Direction, EventKind
from uart_proxy.core.session import UartSession
from uart_proxy.io.socket_source import SocketSource
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource


@pytest.fixture(autouse=True)
def no_real_config(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "CONFIG_PATH", str(tmp_path / "absent.toml"))


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Rig:
    """A serving session on a fake device, and client sessions attached to it."""

    def __init__(self, *, echo_tx: bool) -> None:
        self.device = FakeSource()
        self.session = UartSession(self.device, default_eol=b"\r")
        self.server = ProxyServer(
            self.session, {"rw": Role.FULL, "ro": Role.READONLY},
            host="127.0.0.1", port=0, echo_tx=echo_tx)
        self.server.start()
        self.session.start()
        self.clients: list[tuple[UartSession, list]] = []

    def client(self, code="rw", *, handle_echo=True):
        source = SocketSource("127.0.0.1", self.server.port, code)
        session = UartSession(source, default_eol=b"\r")
        if handle_echo:
            source.on_tx_echo = session.publish_remote_tx
        seen: list = []
        session.bus.subscribe(seen.append)
        session.start()
        assert _wait_for(lambda: session.is_connected)
        assert _wait_for(lambda: self.server.client_count == len(self.clients) + 1)
        self.clients.append((session, seen))
        return session, seen

    def close(self):
        for session, _ in self.clients:
            session.stop()
        self.server.stop()
        self.session.stop()


@pytest.fixture
def rig(request):
    made = []

    def make(**kw):
        r = Rig(**kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.close()


def _tx_lines(seen, *, remote=None):
    return [e.text for e in seen
            if e.kind is EventKind.LINE and e.direction is Direction.TX
            and (remote is None or bool(e.meta.get("remote")) is remote)]


def test_off_by_default_nobody_sees_what_was_typed(rig):
    r = rig(echo_tx=False)
    _, seen = r.client()
    r.session.send_text("secret")
    time.sleep(0.3)
    assert _tx_lines(seen) == []


def test_when_on_a_line_typed_at_the_server_reaches_clients(rig):
    r = rig(echo_tx=True)
    _, seen = r.client()
    r.session.send_text("reboot")
    assert _wait_for(lambda: _tx_lines(seen, remote=True) == ["reboot"])


def test_the_client_that_typed_it_is_not_sent_its_own_line(rig):
    r = rig(echo_tx=True)
    alice, alice_seen = r.client()
    _, bob_seen = r.client()
    alice.send_text("ls")
    assert _wait_for(lambda: _tx_lines(bob_seen, remote=True) == ["ls"])
    time.sleep(0.2)
    assert _tx_lines(alice_seen, remote=True) == [], "no echo of your own line"
    assert _tx_lines(alice_seen, remote=False) == ["ls"], "your own, shown once"
    assert r.device.writes == [b"ls\r"]


def test_keystrokes_are_echoed_as_the_line_not_one_by_one(rig):
    """Character mode sends a byte per key; the echo is the finished line."""
    r = rig(echo_tx=True)
    alice, _ = r.client()
    _, bob_seen = r.client()
    for byte in b"ls\r":
        alice.write(bytes([byte]))
    assert _wait_for(lambda: _tx_lines(bob_seen, remote=True) == ["ls"])
    time.sleep(0.2)
    assert _tx_lines(bob_seen, remote=True) == ["ls"]


def test_a_readonly_viewer_sees_it_too(rig):
    r = rig(echo_tx=True)
    _, viewer_seen = r.client("ro")
    r.session.send_text("uptime")
    assert _wait_for(lambda: _tx_lines(viewer_seen, remote=True) == ["uptime"])


def test_an_echoed_line_is_shown_not_resent(rig):
    """It already reached the device; the client must not write it again."""
    r = rig(echo_tx=True)
    _, _seen = r.client()
    r.session.send_text("once")
    time.sleep(0.4)
    assert r.device.writes == [b"once\r"]


def test_a_client_that_does_not_know_tx_echo_just_ignores_it(rig):
    """Older clients: the new message must not break the RX stream."""
    r = rig(echo_tx=True)
    _, seen = r.client(handle_echo=False)
    r.session.send_text("ignored")
    r.device.feed(b"still streaming\n")
    assert _wait_for(lambda: any(
        e.kind is EventKind.LINE and e.direction is Direction.RX
        and e.text == "still streaming" for e in seen))
    assert _tx_lines(seen) == []


def test_the_flag_reaches_the_server():
    args = argparse.Namespace(serve=True, auth=["rw"], listen="127.0.0.1",
                              listen_port=0, replay_lines=0, echo_tx=True)
    assert _maybe_build_proxy(UartSession(FakeSource()), args).echo_tx is True
    parsed = cli.build_parser().parse_args(["connect", "--port", "/dev/x"])
    assert parsed.echo_tx is False
    parsed = cli.build_parser().parse_args(["start", "--port", "/dev/x", "--echo-tx"])
    assert parsed.echo_tx is True


@pytest.mark.parametrize("eol", [b"\r", b"\r\n", b"\n"])
def test_a_typed_line_is_a_tx_line_whatever_enter_sends(eol):
    session = UartSession(FakeSource(), default_eol=eol)
    seen = []
    session.bus.subscribe(seen.append)
    session.start()
    try:
        assert _wait_for(lambda: session.is_connected)
        session.send_text("ls")
        session.send_text("pwd")
        assert _wait_for(lambda: _tx_lines(seen) == ["ls", "pwd"])
    finally:
        session.stop()
