"""S36: a telnet server as the device — negotiation, framing, window size."""

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
from uart_proxy.io.telnet_source import (
    BINARY, DO, DONT, ECHO, GA, IAC, NAWS, NOP, SB, SE, SGA, TTYPE, WILL, WONT,
    TelnetProtocol, TelnetSource, parse_telnet_url,
)
from uart_proxy.io.url_source import check_port_url

LINEMODE = 34


def b(*values) -> bytes:
    return bytes(values)


# ── the protocol, byte by byte ──────────────────────────────────────────────


def test_plain_data_passes_through():
    assert TelnetProtocol().receive(b"login: ") == (b"login: ", b"")


def test_an_escaped_iac_is_one_0xff():
    assert TelnetProtocol().receive(b(0x41, IAC, IAC, 0x42)) == (b"A\xffB", b"")


def test_the_server_may_echo_and_suppress_go_ahead():
    proto = TelnetProtocol()
    assert proto.receive(b(IAC, WILL, ECHO, IAC, WILL, SGA)) == \
        (b"", b(IAC, DO, ECHO, IAC, DO, SGA))
    assert {ECHO, SGA} <= proto.them


def test_a_repeated_offer_is_not_answered_again():
    """RFC 1143: answering a non-change is how two sides loop forever."""
    proto = TelnetProtocol()
    proto.receive(b(IAC, WILL, ECHO))
    assert proto.receive(b(IAC, WILL, ECHO)) == (b"", b"")


def test_anything_else_the_server_offers_is_refused():
    assert TelnetProtocol().receive(b(IAC, WILL, LINEMODE)) == (b"", b(IAC, DONT, LINEMODE))


def test_asked_for_our_size_we_say_yes_and_send_it():
    proto = TelnetProtocol(size=(132, 43))
    _, reply = proto.receive(b(IAC, DO, NAWS))
    assert reply == b(IAC, WILL, NAWS) + b(IAC, SB, NAWS, 0, 132, 0, 43, IAC, SE)


def test_asked_for_our_terminal_type_we_send_it():
    proto = TelnetProtocol()
    _, reply = proto.receive(b(IAC, DO, TTYPE, IAC, SB, TTYPE, 1, IAC, SE))
    assert reply == b(IAC, WILL, TTYPE) + b(IAC, SB, TTYPE, 0) + b"XTERM-256COLOR" + b(IAC, SE)


def test_what_we_will_not_do_is_refused():
    assert TelnetProtocol().receive(b(IAC, DO, LINEMODE)) == (b"", b(IAC, WONT, LINEMODE))


def test_withdrawals_are_confirmed_only_for_what_was_on():
    proto = TelnetProtocol()
    assert proto.receive(b(IAC, WONT, ECHO)) == (b"", b""), "was never on"
    proto.receive(b(IAC, WILL, ECHO))
    assert proto.receive(b(IAC, WONT, ECHO)) == (b"", b(IAC, DONT, ECHO))
    assert proto.receive(b(IAC, DONT, NAWS)) == (b"", b"")


def test_commands_with_nothing_to_do_are_dropped():
    assert TelnetProtocol().receive(b(0x41, IAC, NOP, IAC, GA, 0x42)) == (b"AB", b"")


def test_negotiation_split_across_reads_is_understood():
    proto = TelnetProtocol()
    first = proto.receive(b(0x41, IAC))
    second = proto.receive(b(WILL))
    third = proto.receive(b(ECHO, 0x42))
    assert first == (b"A", b"") and second == (b"", b"")
    assert third == (b"B", b(IAC, DO, ECHO))


def test_a_subnegotiation_split_across_reads_is_understood():
    proto = TelnetProtocol()
    proto.receive(b(IAC, DO, TTYPE))
    assert proto.receive(b(IAC, SB, TTYPE)) == (b"", b"")
    data, reply = proto.receive(b(1, IAC, SE) + b"x")
    assert data == b"x" and b"XTERM-256COLOR" in reply


def test_cr_nul_is_a_cr_even_across_reads():
    proto = TelnetProtocol()
    assert proto.receive(b"a\r\x00b") == (b"a\rb", b"")
    assert proto.receive(b"c\r")[0] == b"c\r"
    assert proto.receive(b"\x00d")[0] == b"d"


def test_in_binary_mode_a_nul_after_cr_is_data():
    proto = TelnetProtocol()
    proto.receive(b(IAC, WILL, BINARY))
    assert proto.receive(b"a\r\x00b")[0] == b"a\r\x00b"


def test_sending_escapes_iac_and_frames_a_bare_cr():
    proto = TelnetProtocol()
    assert proto.encode(b"guest\r") == b"guest\r\x00"
    assert proto.encode(b"ls\r\n") == b"ls\r\n", "CRLF is already legal"
    assert proto.encode(b"\xff") == b"\xff\xff"


def test_in_binary_mode_a_bare_cr_is_sent_as_is():
    proto = TelnetProtocol()
    proto.receive(b(IAC, DO, BINARY))
    assert proto.encode(b"x\r") == b"x\r"


def test_a_size_containing_255_is_escaped():
    proto = TelnetProtocol(size=(255, 24))
    assert proto.naws() == b(IAC, SB, NAWS, 0, 255, 255, 0, 24, IAC, SE)


def test_a_resize_is_sent_only_once_the_server_asked_for_sizes():
    proto = TelnetProtocol()
    assert proto.resize(100, 30) == b""
    proto.receive(b(IAC, DO, NAWS))
    assert proto.resize(120, 40) == b(IAC, SB, NAWS, 0, 120, 0, 40, IAC, SE)


# ── URLs ────────────────────────────────────────────────────────────────────


def test_urls():
    assert parse_telnet_url("telnet://ptt.cc") == ("ptt.cc", 23)
    assert parse_telnet_url("telnet://lab:2323") == ("lab", 2323)
    assert check_port_url("telnet://ptt.cc") is None
    assert "needs a host" in check_port_url("telnet://")
    assert device_stem("telnet://ptt.cc") == "telnet-ptt.cc"


def test_description():
    assert TelnetSource("telnet://ptt.cc", size=(80, 24)).description() == \
        "telnet ptt.cc (80×24)"
    assert TelnetSource("telnet://lab:2323").description().startswith("telnet lab:2323")


# ── against a telnet server ─────────────────────────────────────────────────


def _wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TelnetServer:
    """Negotiates like a BBS on connect, then greets and records what it gets."""

    OPENING = (b(IAC, WILL, ECHO, IAC, WILL, SGA, IAC, DO, NAWS, IAC, DO, TTYPE,
                 IAC, SB, TTYPE, 1, IAC, SE) + b"login: ")

    def __init__(self) -> None:
        self.received = bytearray()
        self.conn = None
        self.connections = 0
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            self.conn = conn
            self.connections += 1
            conn.sendall(self.OPENING)
            while True:
                try:
                    data = conn.recv(4096)
                except OSError:
                    break
                if not data:
                    break
                self.received.extend(data)
            self.conn = None

    def drop(self):
        # The serving thread clears self.conn as soon as shutdown() wakes it.
        conn = self.conn
        if conn is not None:
            conn.shutdown(socket.SHUT_RDWR)
            conn.close()

    def close(self):
        self.drop()
        self._srv.close()


@pytest.fixture
def server():
    srv = TelnetServer()
    yield srv
    srv.close()


def _session(url, **kw):
    session = UartSession(TelnetSource(url, **kw), default_eol=b"\r",
                          reconnect_interval=0.05)
    rx = bytearray()
    session.bus.subscribe(lambda e: rx.extend(e.data)
                          if e.kind is EventKind.DATA and e.direction is Direction.RX
                          else None)
    return session, rx


def test_negotiation_is_answered_and_never_reaches_the_screen(server):
    session, rx = _session(f"telnet://127.0.0.1:{server.port}", size=(80, 24))
    session.start()
    try:
        assert _wait_for(lambda: bytes(rx) == b"login: "), f"got {bytes(rx)!r}"
        assert _wait_for(lambda: b"XTERM-256COLOR" in bytes(server.received))
        got = bytes(server.received)
        assert b(IAC, DO, ECHO) in got and b(IAC, DO, SGA) in got
        assert b(IAC, WILL, NAWS) + b(IAC, SB, NAWS, 0, 80, 0, 24, IAC, SE) in got
    finally:
        session.stop()


def test_typing_is_framed_for_telnet(server):
    session, rx = _session(f"telnet://127.0.0.1:{server.port}")
    session.start()
    try:
        assert _wait_for(lambda: b"login: " in bytes(rx))
        session.send_text("guest")
        assert _wait_for(lambda: bytes(server.received).endswith(b"guest\r\x00"))
    finally:
        session.stop()


def test_a_resize_is_sent_as_naws(server):
    source = TelnetSource(f"telnet://127.0.0.1:{server.port}")
    session = UartSession(source, reconnect_interval=0.05)
    session.start()
    try:
        assert _wait_for(lambda: NAWS in source.protocol.us)
        source.set_window_size(132, 43)
        assert _wait_for(lambda: bytes(server.received).endswith(
            b(IAC, SB, NAWS, 0, 132, 0, 43, IAC, SE)))
    finally:
        session.stop()


def test_a_fixed_size_is_never_changed(server):
    source = TelnetSource(f"telnet://127.0.0.1:{server.port}", size=(80, 24))
    session = UartSession(source, reconnect_interval=0.05)
    session.start()
    try:
        assert _wait_for(lambda: NAWS in source.protocol.us)
        before = len(server.received)
        source.set_window_size(200, 60)
        time.sleep(0.3)
        assert len(server.received) == before and source.size == (80, 24)
    finally:
        session.stop()


def test_a_dropped_connection_reconnects_and_negotiates_afresh(server):
    session, rx = _session(f"telnet://127.0.0.1:{server.port}")
    session.start()
    try:
        assert _wait_for(lambda: bytes(rx).count(b"login: ") == 1)
        server.drop()
        assert _wait_for(lambda: bytes(rx).count(b"login: ") == 2, timeout=10)
        assert server.connections == 2
        assert bytes(server.received).count(b(IAC, DO, ECHO)) == 2, \
            "a new connection is a new negotiation"
    finally:
        session.stop()


# ── the CLI ─────────────────────────────────────────────────────────────────


def test_connect_builds_a_telnet_source_in_character_mode(monkeypatch):
    seen = {}

    def fake_run(session, args, **kw):
        seen["source"], seen["input"] = session.source, args.input
        return 0

    monkeypatch.setattr(cli, "_run_session", fake_run)
    args = cli.build_parser().parse_args(["connect", "--port", "telnet://ptt.cc",
                                          "--term-size", "80x24", "--no-log"])
    assert cli.cmd_connect(args) == 0
    assert isinstance(seen["source"], TelnetSource)
    assert seen["source"].fixed_size and seen["input"] == "char"


def test_connect_over_telnet_end_to_end(server, tmp_path):
    logs = tmp_path / "logs"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port",
         f"telnet://127.0.0.1:{server.port}", "--no-tui", "--output-dir", str(logs)],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, HOME=str(tmp_path)))
    try:
        raw = logs / "output.log"
        assert _wait_for(lambda: raw.exists() and b"login: " in raw.read_bytes(), 15)
    finally:
        proc.terminate()
        out, _ = proc.communicate(timeout=10)
    assert raw.read_bytes() == b"login: ", "no negotiation bytes in the device log"
    assert "telnet — option negotiation handled" in out.decode()
