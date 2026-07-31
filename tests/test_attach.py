"""S18: replay over the wire, and `attach` end to end."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from uart_proxy.core import daemon as daemon_mod
from uart_proxy.core.daemon import DAEMON_SUPPORTED, find_daemon, list_daemons
from uart_proxy.core.replay import ReplayBuffer
from uart_proxy.core.session import UartSession
from uart_proxy.io.socket_source import SocketSource
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource

CODE = "123456"


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture
def served():
    """A running session + proxy with history, on an ephemeral port."""
    source = FakeSource()
    session = UartSession(source, auto_reconnect=False)
    replay = ReplayBuffer(max_lines=100)
    session.bus.subscribe(replay.handle)
    server = ProxyServer(session, {CODE: Role.FULL}, host="127.0.0.1", port=0,
                         replay=replay)
    server.start()
    session.start()
    assert _wait(lambda: session.is_connected)
    try:
        yield session, source, server, replay
    finally:
        server.stop()
        session.stop()


def _feed(source: FakeSource, replay: ReplayBuffer, lines: list[str]) -> None:
    source.feed(("".join(f"{line}\n" for line in lines)).encode())
    assert _wait(lambda: len(replay) >= len(lines)), "history did not accumulate"


# ── over the wire ───────────────────────────────────────────────────────────


def test_a_client_that_asks_gets_the_history_first(served):
    """It must arrive before any live traffic, so everything after the block can
    be relied on as the present."""
    session, source, server, replay = served
    _feed(source, replay, ["boot 1", "boot 2", "boot 3"])

    client = SocketSource("127.0.0.1", server.port, CODE, replay_lines=50)
    client.open()
    try:
        assert [e.text for e in client.replay] == ["boot 1", "boot 2", "boot 3"]
        assert client.replay_available == 3
        # …and the live stream still works afterwards.
        source.feed(b"live\n")
        got = b""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and b"live" not in got:
            got += client.read(4096, timeout=0.2)
        assert b"live" in got
        assert "live" not in [e.text for e in client.replay], "not replayed twice"
    finally:
        client.close()


def test_history_carries_the_servers_own_timestamps(served):
    session, source, server, replay = served
    _feed(source, replay, ["stamped"])
    expected = replay.snapshot()[0]

    client = SocketSource("127.0.0.1", server.port, CODE, replay_lines=50)
    client.open()
    try:
        entry = client.replay[0]
        assert entry.wall == expected.wall
        assert entry.elapsed == pytest.approx(expected.elapsed, abs=0.001)
    finally:
        client.close()


def test_a_client_that_does_not_ask_gets_none(served):
    """The default is live-only, so nothing changes for existing clients."""
    session, source, server, replay = served
    _feed(source, replay, ["old news"])

    client = SocketSource("127.0.0.1", server.port, CODE)  # replay_lines=0
    client.open()
    try:
        assert client.replay == []
        assert client.replay_available == 1, "still told what was available"
    finally:
        client.close()


def test_asking_for_fewer_lines_is_honoured(served):
    session, source, server, replay = served
    _feed(source, replay, [f"line {i}" for i in range(10)])

    client = SocketSource("127.0.0.1", server.port, CODE, replay_lines=3)
    client.open()
    try:
        assert [e.text for e in client.replay] == ["line 7", "line 8", "line 9"]
    finally:
        client.close()


def test_a_server_without_history_is_not_a_broken_one(served):
    """`connect --replay-lines 0`, or an older server: a client that asked gets
    an empty block rather than hanging."""
    source = FakeSource()
    session = UartSession(source, auto_reconnect=False)
    server = ProxyServer(session, {CODE: Role.FULL}, host="127.0.0.1", port=0)
    server.start()
    session.start()
    try:
        # A short timeout on purpose: a current server must answer with an empty
        # `replay_end` rather than leave the client waiting it out.
        client = SocketSource("127.0.0.1", server.port, CODE, replay_lines=50,
                              replay_timeout=1.5)
        client.open()
        try:
            assert client.replay == []
            assert client.replay_available == 0
        finally:
            client.close()
    finally:
        server.stop()
        session.stop()


def test_the_client_adopts_the_servers_elapsed_clock(served):
    session, source, server, replay = served
    client = SocketSource("127.0.0.1", server.port, CODE, replay_lines=10)
    client.open()
    try:
        assert client.remote_elapsed is not None
        assert client.remote_elapsed == pytest.approx(
            session.tracker.stamp().elapsed, abs=5.0)
    finally:
        client.close()


def test_opening_twice_keeps_one_connection(served):
    """`attach` connects eagerly to get the history, then the session's manager
    calls open() again — that must not reconnect and lose the replay."""
    session, source, server, replay = served
    _feed(source, replay, ["once"])
    client = SocketSource("127.0.0.1", server.port, CODE, replay_lines=10)
    client.open()
    try:
        # The server registers a client on its own handler thread, so wait for it
        # rather than racing it.
        assert _wait(lambda: server.client_count == 1)
        before = len(client.replay)
        client.open()  # the manager's call
        assert len(client.replay) == before == 1
        time.sleep(0.3)  # long enough for a stray connection to show up
        assert server.client_count == 1, "a second connection was made"
    finally:
        client.close()


# ── end to end through the CLI ──────────────────────────────────────────────


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(daemon_mod.HOME_ENV, str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="needs POSIX fork/setsid")
def test_attach_shows_what_happened_while_nobody_watched(tmp_path, isolated_home):
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    device = os.ttyname(slave)
    env = dict(os.environ, **{daemon_mod.HOME_ENV: str(isolated_home)})

    def cli(*args, **kw):
        return subprocess.run([sys.executable, "-m", "uart_proxy", *args],
                              env=env, capture_output=True, text=True,
                              timeout=60, **kw)

    started = cli("start", "--port", device, "--name", "probe",
                  "--output-dir", str(tmp_path / "logs"), "--listen-port", "0")
    os.close(slave)
    attached = None
    try:
        assert started.returncode == 0, started.stderr
        assert _wait(lambda: find_daemon("probe").is_alive)

        # The device talks with nobody attached.
        time.sleep(1.0)
        os.write(master, b"[boot] while you were out\r\n")
        time.sleep(0.6)

        attached = subprocess.Popen(
            [sys.executable, "-m", "uart_proxy", "attach", "probe",
             "--no-tui", "--timestamp", "full"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )
        # Wait for the client to say it is live rather than sleeping and hoping:
        # on a loaded machine a fixed sleep can expire before it has connected,
        # and the "live" line would then be written while nobody is subscribed.
        collected: list[str] = []
        reader = threading.Thread(
            target=lambda: collected.extend(iter(attached.stdout.readline, "")),
            daemon=True,
        )
        reader.start()
        assert _wait(lambda: any("── live ──" in line for line in collected),
                     timeout=30), f"never went live:\n{''.join(collected)}"

        os.write(master, b"[now] after attaching\r\n")
        assert _wait(lambda: any("after attaching" in line for line in collected),
                     timeout=30), f"live output never arrived:\n{''.join(collected)}"
        attached.terminate()
        attached.wait(timeout=15)
        reader.join(timeout=5)
        out, err = "".join(collected), attached.stderr.read()

        assert "── replayed" in out, f"no history block:\n{out}\n{err}"
        assert "while you were out" in out, "the missed line was not replayed"
        assert "── live ──" in out
        assert "after attaching" in out, "live traffic stopped flowing"
        # History above the divider, live below it.
        assert out.index("while you were out") < out.index("── live ──")
        assert out.index("── live ──") < out.index("after attaching")
    finally:
        if attached is not None and attached.poll() is None:
            attached.kill()
        cli("stop", "--all")
        for info in list_daemons():
            try:
                os.kill(info.pid, signal.SIGKILL)
            except OSError:
                pass
        os.close(master)


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="needs POSIX fork/setsid")
def test_attach_without_a_session_explains_itself(isolated_home):
    env = dict(os.environ, **{daemon_mod.HOME_ENV: str(isolated_home)})
    result = subprocess.run([sys.executable, "-m", "uart_proxy", "attach"],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    assert "no session is running" in result.stderr
