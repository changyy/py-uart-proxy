"""S22: a guessable code is never the default, and guessing is rate-limited."""

from __future__ import annotations

import argparse
import os
import sys
import time

import pytest

from uart_proxy.cli import _maybe_build_proxy
from uart_proxy.core.events import EventKind
from uart_proxy.core.session import UartSession
from uart_proxy.io.socket_source import SocketSource, SocketSourceError
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import AuthLimiter, ProxyServer

from conftest import FakeSource


@pytest.fixture(autouse=True)
def no_real_config(tmp_path, monkeypatch):
    """Never read the developer's own ~/.uart-proxy/config.toml."""
    from uart_proxy import cli as _cli

    monkeypatch.setattr(_cli, "CONFIG_PATH", str(tmp_path / "absent.toml"))


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


# ── the limiter on its own ──────────────────────────────────────────────────


def _limiter(**kw):
    clock = Clock()
    return AuthLimiter(clock=clock, **{"max_failures": 3, "window": 60, "ban": 600, **kw}), clock


def test_an_address_is_refused_after_too_many_failures():
    limiter, _ = _limiter()
    assert [limiter.record_failure("10.0.0.9") for _ in range(3)] == [False, False, True]
    assert limiter.banned_for("10.0.0.9") == pytest.approx(600)


def test_the_refusal_is_only_for_that_address():
    """A lockout for everyone is one a stranger could trigger at will."""
    limiter, _ = _limiter()
    for _ in range(3):
        limiter.record_failure("10.0.0.9")
    assert limiter.banned_for("10.0.0.2") == 0


def test_the_refusal_expires_without_a_restart():
    limiter, clock = _limiter()
    for _ in range(3):
        limiter.record_failure("10.0.0.9")
    clock.now += 599
    assert limiter.banned_for("10.0.0.9") > 0
    clock.now += 2
    assert limiter.banned_for("10.0.0.9") == 0


def test_failures_spread_out_beyond_the_window_are_forgiven():
    limiter, clock = _limiter()
    for _ in range(10):
        assert limiter.record_failure("10.0.0.9") is False
        clock.now += 31  # never three inside one 60 s window


def test_a_right_code_clears_earlier_typos():
    limiter, _ = _limiter()
    limiter.record_failure("10.0.0.9")
    limiter.record_failure("10.0.0.9")
    limiter.record_success("10.0.0.9")
    assert limiter.record_failure("10.0.0.9") is False


# ── through the real server ─────────────────────────────────────────────────


def _server(limiter):
    session = UartSession(FakeSource())
    server = ProxyServer(session, {"right-code": Role.FULL},
                         host="127.0.0.1", port=0, limiter=limiter)
    notices: list[str] = []
    session.bus.subscribe(
        lambda e: notices.append(e.text) if e.kind is EventKind.NOTICE else None)
    server.start()
    session.start()
    return session, server, notices


def _try(server, code):
    client = SocketSource("127.0.0.1", server.port, code)
    try:
        client.open()
    finally:
        client.close()


def test_guessing_gets_the_address_refused_even_with_the_right_code():
    limiter = AuthLimiter(max_failures=3, window=60, ban=600)
    session, server, notices = _server(limiter)
    try:
        for _ in range(3):
            with pytest.raises(SocketSourceError, match="invalid code"):
                _try(server, "guess")
        assert any("refusing 127.0.0.1 for 10 min" in n for n in notices)

        with pytest.raises(SocketSourceError, match=r"try again in \d+s"):
            _try(server, "right-code")
    finally:
        server.stop()
        session.stop()


def test_a_client_that_knows_the_code_is_unaffected():
    limiter = AuthLimiter(max_failures=3, window=60, ban=600)
    session, server, notices = _server(limiter)
    try:
        for _ in range(2):
            with pytest.raises(SocketSourceError):
                _try(server, "guess")
            _try(server, "right-code")
        assert limiter.banned_for("127.0.0.1") == 0 and notices == []
    finally:
        server.stop()
        session.stop()


def test_a_malformed_hello_counts_as_a_failure():
    import socket

    limiter = AuthLimiter(max_failures=2, window=60, ban=600)
    session, server, _ = _server(limiter)
    try:
        for _ in range(2):
            with socket.create_connection(("127.0.0.1", server.port), timeout=3) as s:
                s.sendall(b"not json\n")
                s.recv(4096)
        assert limiter.banned_for("127.0.0.1") > 0
    finally:
        server.stop()
        session.stop()


# ── the default code ────────────────────────────────────────────────────────


def _args(**kw):
    return argparse.Namespace(serve=True, auth=kw.get("auth"), listen="127.0.0.1",
                              listen_port=0, replay_lines=0)


def test_serve_without_auth_generates_a_code_nobody_could_guess(capsys):
    codes = []
    for _ in range(2):
        proxy = _maybe_build_proxy(UartSession(FakeSource()), _args())
        (code,) = proxy.auth
        assert proxy.auth[code] is Role.FULL
        codes.append(code)
    assert "123456" not in codes
    assert codes[0] != codes[1], "a fresh code per run"
    assert len(codes[0]) >= 16
    err = capsys.readouterr().err
    assert f"generated code {codes[0]}" in err and f"generated code {codes[1]}" in err


def test_a_given_code_is_used_as_is(capsys):
    proxy = _maybe_build_proxy(UartSession(FakeSource()),
                               _args(auth=["123456", "000000:readonly"]))
    assert proxy.auth == {"123456": Role.FULL, "000000": Role.READONLY}
    assert "generated" not in capsys.readouterr().err


# ── a refused client stops instead of retrying itself into a ban ────────────


def _wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class _RefusedOnReconnect(FakeSource):
    """Opens once, then — after a drop — is refused every time."""

    def open(self) -> None:
        from uart_proxy.io.source import SourceRefused

        self.open_calls += 1
        if self.open_calls > 1:
            raise SourceRefused("invalid code")
        self.opened = True


def test_a_refusal_ends_the_session_instead_of_a_retry_loop():
    source = _RefusedOnReconnect()
    session = UartSession(source, reconnect_interval=0.01)
    events = []
    session.bus.subscribe(events.append)
    session.start()
    assert _wait_for(lambda: session.is_connected)
    source.drop()
    assert _wait_for(lambda: not session.is_running)
    time.sleep(0.1)
    assert source.open_calls == 2, "one attempt after the drop, not a loop"
    statuses = [(e.text, e.meta) for e in events if e.kind is EventKind.STATUS]
    assert ("error", {"source": source.description(), "error": "invalid code",
                      "refused": True}) in statuses
    assert statuses[-1] == ("disconnected", {"reason": "refused"})
    session.stop()
    assert [t for t, _ in statuses].count("disconnected") == 1


def test_a_wrong_code_raises_a_refusal_not_a_plain_error():
    from uart_proxy.io.socket_source import AuthRefused
    from uart_proxy.io.source import SourceRefused

    session, server, _ = _server(AuthLimiter())
    try:
        with pytest.raises(AuthRefused) as caught:
            _try(server, "guess")
        assert isinstance(caught.value, SourceRefused)
        assert isinstance(caught.value, SocketSourceError), "old callers still catch it"
    finally:
        server.stop()
        session.stop()


def test_an_unreachable_server_is_still_retried():
    """Absence is not refusal: a server that is down may come back."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # closed again: nothing listens here
    client = SocketSource("127.0.0.1", port, "code", connect_timeout=0.5)
    session = UartSession(client, reconnect_interval=0.01)
    session.start()
    try:
        time.sleep(0.3)
        assert session.is_running, "a refused connection is not an auth refusal"
    finally:
        session.stop()


def test_a_server_restarted_with_a_new_code_does_not_ban_its_old_clients():
    """The realistic trap: `connect --serve` restarted generates a fresh code,
    and every client still holding the old one reconnects with it."""
    device = FakeSource()
    first = UartSession(device)
    server = ProxyServer(first, {"old": Role.FULL}, host="127.0.0.1", port=0)
    server.start()
    first.start()
    port = server.port

    client = SocketSource("127.0.0.1", port, "old")
    session = UartSession(client, reconnect_interval=0.05)
    session.start()
    second = None
    try:
        assert _wait_for(lambda: session.is_connected)
        server.stop()
        first.stop()

        limiter = AuthLimiter(max_failures=3, window=60, ban=600)
        second = UartSession(FakeSource())
        server = ProxyServer(second, {"new": Role.FULL}, host="127.0.0.1",
                             port=port, limiter=limiter)
        server.start()
        second.start()

        assert _wait_for(lambda: not session.is_running)
        time.sleep(0.3)
        assert limiter.banned_for("127.0.0.1") == 0, "retried itself into a ban"
    finally:
        session.stop()
        server.stop()
        if second is not None:
            second.stop()


# ── fixed codes from config.toml (S24) ──────────────────────────────────────


from uart_proxy import cli  # noqa: E402

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    monkeypatch.setattr(cli, "CONFIG_PATH", str(path))

    def write(text: str, mode: int = 0o600):
        path.write_text(text)
        os.chmod(path, mode)
        return path

    return write


def _codes(capsys, **kw):
    proxy = _maybe_build_proxy(UartSession(FakeSource()), _args(**kw))
    return {c: r.value for c, r in proxy.auth.items()}, capsys.readouterr().err


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_config_codes_are_used_when_none_are_given(config, capsys):
    config('[proxy]\nauth = ["fixedcode", "look:readonly"]\n')
    codes, err = _codes(capsys)
    assert codes == {"fixedcode": "full", "look": "readonly"}
    assert "from" in err and "config.toml" in err and "generated" not in err


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_a_single_string_is_one_code(config, capsys):
    config('[proxy]\nauth = "fixedcode"\n')
    assert _codes(capsys)[0] == {"fixedcode": "full"}


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_the_command_line_beats_the_config(config, capsys):
    config('[proxy]\nauth = ["fixedcode"]\n')
    codes, err = _codes(capsys, auth=["given"])
    assert codes == {"given": "full"}
    assert "config.toml" not in err


@posix_only
@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_a_config_others_can_read_is_not_trusted_with_a_code(config, capsys):
    config('[proxy]\nauth = ["fixedcode"]\n', mode=0o644)
    codes, err = _codes(capsys)
    assert "fixedcode" not in codes
    assert "readable by others" in err and "chmod 600" in err
    assert "generated code" in err, "fails safe, to a random code"


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
@pytest.mark.parametrize("value", ["42", "[1, 2]", '[""]', "{a = 1}"])
def test_a_malformed_setting_is_a_note_and_a_generated_code(config, capsys, value):
    config(f"[proxy]\nauth = {value}\n")
    codes, err = _codes(capsys)
    assert len(codes) == 1 and "must be a list" in err and "generated code" in err


def test_no_config_file_means_a_generated_code(config, capsys):
    codes, err = _codes(capsys)
    assert len(codes) == 1 and "generated code" in err


@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_a_config_without_a_proxy_section_changes_nothing(config, capsys):
    config("[retention]\nmax_age_days = 3\n")
    codes, err = _codes(capsys)
    assert len(codes) == 1 and "generated code" in err and "Note" not in err


def test_python_without_tomllib_says_the_config_was_skipped(config, capsys, monkeypatch):
    config('[proxy]\nauth = ["fixedcode"]\n')
    monkeypatch.setitem(sys.modules, "tomllib", None)  # import now raises
    codes, err = _codes(capsys)
    assert "fixedcode" not in codes
    assert "3.11" in err and "generated code" in err


@posix_only
@pytest.mark.skipif(sys.version_info < (3, 11), reason="tomllib")
def test_a_background_session_takes_its_codes_from_the_config_too(tmp_path):
    """`start` resolves codes the same way, and records every one of them."""
    import json
    import pty
    import subprocess

    from uart_proxy.core.daemon import DAEMON_SUPPORTED

    if not DAEMON_SUPPORTED:
        pytest.skip("needs fork")
    home = tmp_path / "userhome"
    (home / ".uart-proxy").mkdir(parents=True)
    cfg = home / ".uart-proxy" / "config.toml"
    cfg.write_text('[proxy]\nauth = ["look:readonly", "fixedcode"]\n')
    os.chmod(cfg, 0o600)
    env = dict(os.environ, HOME=str(home), UART_PROXY_HOME=str(tmp_path / "state"))
    master, slave = pty.openpty()

    def run(*argv):
        return subprocess.run([sys.executable, "-m", "uart_proxy", *argv],
                              capture_output=True, text=True, env=env, timeout=60)

    try:
        started = run("start", "--port", os.ttyname(slave), "--name", "cfg",
                      "--no-log", "--listen-port", "0")
        assert started.returncode == 0, started.stderr
        (entry,) = json.loads(run("status", "--json", "--show-auth").stdout)["data"]
        assert entry["auth"] == {"look": "readonly", "fixedcode": "full"}
        # attach uses the full-access one even though it was listed second.
        from uart_proxy.core import daemon as daemon_mod

        info = daemon_mod.read_state(
            str(tmp_path / "state" / "daemons" / "cfg.json"))
        assert info.auth == "fixedcode"
    finally:
        run("stop", "--all")
        os.close(master)
        os.close(slave)


# ── connection cap (S26) ────────────────────────────────────────────────────


def _capped(max_clients):
    session = UartSession(FakeSource())
    server = ProxyServer(session, {"right-code": Role.FULL}, host="127.0.0.1",
                         port=0, max_clients=max_clients)
    server.start()
    session.start()
    return session, server


def test_a_full_server_turns_the_next_client_away_with_a_retry():
    session, server = _capped(2)
    held = [SocketSource("127.0.0.1", server.port, "right-code") for _ in range(2)]
    try:
        for client in held:
            client.open()
        assert _wait_for(lambda: server.client_count == 2)
        extra = SocketSource("127.0.0.1", server.port, "right-code")
        with pytest.raises(SocketSourceError, match="server full") as caught:
            extra.open()
        from uart_proxy.io.source import SourceRefused

        assert not isinstance(caught.value, SourceRefused), \
            "full is temporary: the client must keep retrying"
    finally:
        for client in held:
            client.close()
        server.stop()
        session.stop()


def test_a_slot_frees_when_a_client_leaves():
    session, server = _capped(1)
    first = SocketSource("127.0.0.1", server.port, "right-code")
    try:
        first.open()
        first.close()
        assert _wait_for(lambda: server._active == 0)
        second = SocketSource("127.0.0.1", server.port, "right-code")
        second.open()
        second.close()
    finally:
        server.stop()
        session.stop()


def test_idle_unauthenticated_sockets_count_against_the_cap():
    """Otherwise sockets that never send `auth` could hold every slot."""
    import socket

    session, server = _capped(1)
    idle = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    try:
        assert _wait_for(lambda: server._active == 1)
        with pytest.raises(SocketSourceError, match="server full"):
            _try(server, "right-code")
    finally:
        idle.close()
        server.stop()
        session.stop()


def test_being_turned_away_is_not_an_auth_failure():
    session, server = _capped(1)
    holder = SocketSource("127.0.0.1", server.port, "right-code")
    try:
        holder.open()
        for _ in range(server.limiter.max_failures + 2):
            with pytest.raises(SocketSourceError):
                _try(server, "right-code")
        assert server.limiter.banned_for("127.0.0.1") == 0
    finally:
        holder.close()
        server.stop()
        session.stop()


def test_zero_means_no_cap():
    session, server = _capped(0)
    clients = [SocketSource("127.0.0.1", server.port, "right-code") for _ in range(20)]
    try:
        for client in clients:
            client.open()
        assert _wait_for(lambda: server.client_count == 20)
    finally:
        for client in clients:
            client.close()
        server.stop()
        session.stop()


def test_a_client_turned_away_keeps_retrying_and_gets_in():
    session, server = _capped(1)
    holder = SocketSource("127.0.0.1", server.port, "right-code")
    holder.open()
    waiting = UartSession(SocketSource("127.0.0.1", server.port, "right-code"),
                          reconnect_interval=0.05)
    try:
        waiting.start()
        time.sleep(0.3)
        assert waiting.is_running and not waiting.is_connected
        holder.close()
        assert _wait_for(lambda: waiting.is_connected)
    finally:
        waiting.stop()
        server.stop()
        session.stop()


def test_the_cap_is_a_flag(capsys):
    args = _args()
    args.max_clients = 3
    proxy = _maybe_build_proxy(UartSession(FakeSource()), args)
    assert proxy.max_clients == 3
    parsed = cli.build_parser().parse_args(["connect", "--port", "/dev/x"])
    from uart_proxy.proxy.server import MAX_CLIENTS

    assert parsed.max_clients == MAX_CLIENTS
