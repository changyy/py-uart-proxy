"""S41: the session client, and `tail` / `expect` / `send`."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

import uart_proxy.core.daemon as daemon
from uart_proxy.client import ReadOnlyError, SessionClient
from uart_proxy.core.daemon import register_served
from uart_proxy.core.replay import ReplayBuffer
from uart_proxy.core.session import UartSession
from uart_proxy.core.text import clean_text
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv(daemon.HOME_ENV, str(home))
    return home


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def served():
    """A served fake device with history, torn down after the test."""
    made = []

    def _make(*, echo=False, history=b""):
        device = FakeSource(echo=echo)
        session = UartSession(device)
        replay = ReplayBuffer(100)
        session.bus.subscribe(replay.handle)
        server = ProxyServer(session, {"rw": Role.FULL, "ro": Role.READONLY},
                             host="127.0.0.1", port=0, replay=replay)
        server.start()
        session.start()
        assert _wait_for(lambda: session.is_connected)
        if history:
            device.feed(history)
            assert _wait_for(lambda: len(replay) >= history.count(b"\n"))
        made.append((session, server))
        return device, session, server

    yield _make
    for session, server in made:
        server.stop()
        session.stop()


def _client(server, code="ro", **kw):
    client = SessionClient("127.0.0.1", server.port, code, client_name="test", **kw)
    client.connect()
    return client


# ── text ────────────────────────────────────────────────────────────────────


def test_s41_text_is_cleaned_for_reading():
    assert clean_text("\x1b[32mok\x1b[0m") == "ok"
    assert clean_text("a\tb\x07\x00") == "a\tb"
    assert clean_text("\x1b]0;title\x07prompt") == "prompt"


# ── lines ───────────────────────────────────────────────────────────────────


def test_s41_replayed_lines_come_first_with_the_servers_stamps(served):
    device, session, server = served(history=b"boot 1\nboot 2\n")
    client = _client(server)
    try:
        lines, cursor, dropped = client.read(0)
        assert [(l["text"], l["replayed"]) for l in lines] == [("boot 1", True), ("boot 2", True)]
        assert dropped == 0 and cursor == lines[-1]["n"]
        assert all(l["wall"] and l["elapsed"].count(":") == 2 for l in lines)
        device.feed(b"\x1b[1mlive\x1b[0m\r\n")
        assert _wait_for(lambda: client.read(cursor)[0])
        new, cursor2, _ = client.read(cursor)
        assert [(l["text"], l["replayed"]) for l in new] == [("live", False)]
        assert new[0]["n"] == cursor + 1 and cursor2 == new[0]["n"]
        assert client.tail(2)[-1]["text"] == "live"
    finally:
        client.close()


def test_s41_a_reader_that_falls_behind_is_told_how_many_it_missed(served):
    device, session, server = served()
    client = _client(server, max_lines=3)
    try:
        device.feed(b"".join(f"line {i}\n".encode() for i in range(1, 8)))
        assert _wait_for(lambda: client.tail(1) and client.tail(1)[0]["text"] == "line 7")
        lines, cursor, dropped = client.read(0)
        assert [l["text"] for l in lines] == ["line 5", "line 6", "line 7"]
        assert dropped == 4
    finally:
        client.close()


def test_s41_invalid_utf8_is_replaced_not_raised(served):
    device, session, server = served()
    client = _client(server)
    try:
        device.feed(b"bad \xff\xfe bytes\n")
        assert _wait_for(lambda: client.tail(1))
        assert client.tail(1)[0]["text"].startswith("bad ")
    finally:
        client.close()


# ── expect ──────────────────────────────────────────────────────────────────


def test_s41_expect_matches_a_line_after_the_call_only(served):
    device, session, server = served(history=b"READY old\n")
    client = _client(server)
    try:
        assert client.expect(r"READY", timeout=0.3) is None, "history is not waited for"
        device.feed(b"noise\nREADY now\n")
        hit = client.expect(r"READY (\w+)", timeout=3)
        assert hit is not None and hit["line"]["text"] == "READY now"
        assert [l["text"] for l in hit["before"]][-1] == "noise"
    finally:
        client.close()


def test_s41_expect_sees_a_prompt_with_no_newline(served):
    device, session, server = served()
    client = _client(server)
    try:
        start = client.cursor
        device.feed(b"\r\nlogin: ")
        hit = client.expect(r"login:\s*$", timeout=3, since=start)
        assert hit is not None and hit["line"]["partial"] is True
    finally:
        client.close()


def test_s41_expect_times_out_cleanly(served):
    device, session, server = served()
    client = _client(server)
    try:
        t = time.monotonic()
        assert client.expect("never", timeout=0.4) is None
        assert 0.35 <= time.monotonic() - t < 2
    finally:
        client.close()


# ── sending ─────────────────────────────────────────────────────────────────


def test_s41_a_readonly_client_cannot_send(served):
    device, session, server = served()
    client = _client(server, "ro")
    try:
        with pytest.raises(ReadOnlyError):
            client.send_text("reboot")
        with pytest.raises(ReadOnlyError):
            client.send_hex("A5 01")
        time.sleep(0.2)
        assert device.writes == []
    finally:
        client.close()


def test_s41_a_full_client_sends_text_and_hex(served):
    device, session, server = served(echo=True)
    client = _client(server, "rw")
    try:
        mark = client.cursor
        client.send_text("hello", eol="crlf")
        client.send_hex("A5 01 0d 0a")
        assert _wait_for(lambda: b"hello\r\n" in b"".join(device.writes))
        assert _wait_for(lambda: b"\xa5\x01\r\n" in b"".join(device.writes))
        assert client.expect("hello", timeout=3, since=mark) is not None, "the echo comes back"
    finally:
        client.close()


def test_s41_from_registry_takes_the_readonly_code_unless_asked(served):
    device, session, server = served()
    register_served(server, name="bench", port="COM3", baud=115200)
    ro = SessionClient.from_registry("bench", client_name="t")
    rw = SessionClient.from_registry("bench", client_name="t", want_full=True)
    assert (ro.code, rw.code) == ("ro", "rw")


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli(*args, timeout=30):
    return subprocess.run([sys.executable, "-m", "uart_proxy", *args], capture_output=True,
                          text=True, encoding="utf-8", env=os.environ.copy(), timeout=timeout)


def test_s41_cli_send_expect_and_tail(served):
    device, session, server = served(echo=True, history=b"boot ok\n")
    info = register_served(server, name="bench", port="COM3", baud=115200)
    sent = _cli("send", info.name, "ping", "--expect", "ping", "--timeout", "5")
    assert sent.returncode == 0, sent.stderr
    assert "ping" in sent.stdout
    tail = _cli("tail", info.name, "-n", "5")
    assert tail.returncode == 0 and "boot ok" in tail.stdout
    missing = _cli("expect", info.name, "never-says-this", "--timeout", "0.5")
    assert missing.returncode == 1


def test_s41_cli_reaches_a_server_by_address(served):
    device, session, server = served(history=b"hello there\n")
    out = _cli("tail", "--host", "127.0.0.1", "--port", str(server.port), "--auth", "ro")
    assert out.returncode == 0 and "hello there" in out.stdout
