"""S23: the auth code can be looked up again after the console scrolls away."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from uart_proxy import cli
from uart_proxy.cli import (
    _best_code,
    _status_codes,
    register_foreground,
    session_info_lines,
)
from uart_proxy.core import daemon as daemon_mod
from uart_proxy.core.daemon import (
    DAEMON_SUPPORTED,
    DaemonInfo,
    connect_host,
    find_daemon,
    list_daemons,
    unique_name,
)
from uart_proxy.core.events import EventKind
from uart_proxy.core.port_busy import describe_busy
from uart_proxy.core.session import UartSession
from uart_proxy.io.socket_source import SocketSource
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer
from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE

from conftest import FakeSource

posix_only = pytest.mark.skipif(not DAEMON_SUPPORTED, reason="POSIX registry")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.uart-proxy."""
    home = tmp_path / "home"
    monkeypatch.setenv(daemon_mod.HOME_ENV, str(home))
    return home


def _info(name="usbserial-110", **kw) -> DaemonInfo:
    defaults = dict(pid=os.getpid(), port="/dev/tty.usbserial-110", baud=115200,
                    listen_host="127.0.0.1", listen_port=9600, auth="abc",
                    started_at=time.time())
    defaults.update(kw)
    return DaemonInfo(name=name, **defaults)


def _proxy(auth):
    return ProxyServer(UartSession(FakeSource()), auth, host="127.0.0.1", port=0)


def _connect_args(**kw):
    base = dict(port="/dev/tty.usbserial-110", baud=115200, listen="0.0.0.0",
                proxy_dir=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ── which code attach uses ──────────────────────────────────────────────────


def test_attach_gets_the_full_access_code_when_there_is_one():
    assert _best_code({"ro": "readonly", "rw": "full"}) == "rw"


def test_only_readonly_codes_still_give_attach_something():
    assert _best_code({"ro": "readonly"}) == "ro"


def test_no_codes_is_an_empty_code_not_a_crash():
    assert _best_code({}) == ""


# ── the registry additions ──────────────────────────────────────────────────


def test_codes_and_foreground_survive_a_round_trip():
    info = _info(codes={"rw": "full", "ro": "readonly"}, foreground=True)
    info.write()
    loaded = daemon_mod.read_state(info.path)
    assert loaded.codes == {"rw": "full", "ro": "readonly"}
    assert loaded.foreground is True


def test_a_state_file_from_before_codes_still_shows_its_code():
    """Older daemons wrote only `auth`; status must still be able to show it."""
    info = _info(auth="legacy")
    data = {k: v for k, v in info.__dict__.items() if k not in ("codes", "foreground")}
    os.makedirs(daemon_mod.daemon_dir(), exist_ok=True)
    with open(info.path, "w") as fh:
        json.dump(data, fh)
    loaded = daemon_mod.read_state(info.path)
    assert loaded.foreground is False
    assert _status_codes(loaded) == {"legacy": "full"}


def test_a_taken_name_gets_a_suffix():
    assert unique_name("usbserial-110") == "usbserial-110"
    _info("usbserial-110").write()
    assert unique_name("usbserial-110") == "usbserial-110-2"
    _info("usbserial-110-2").write()
    assert unique_name("usbserial-110") == "usbserial-110-3"


def test_a_dead_session_does_not_hold_its_name():
    _info("usbserial-110", pid=2 ** 22 + 12345).write()  # no such pid
    assert unique_name("usbserial-110") == "usbserial-110"


@pytest.mark.parametrize("bound, reach", [
    ("0.0.0.0", "127.0.0.1"), ("", "127.0.0.1"), ("::", "::1"),
    ("127.0.0.1", "127.0.0.1"), ("192.168.1.10", "192.168.1.10"),
])
def test_a_wildcard_bind_is_reached_on_loopback(bound, reach):
    assert connect_host(bound) == reach


# ── registering a foreground --serve ────────────────────────────────────────


@posix_only
def test_a_serving_connect_is_registered_privately():
    proxy = _proxy({"rw": Role.FULL, "ro": Role.READONLY})
    proxy.start()
    try:
        info = register_foreground(_connect_args(proxy_dir="/tmp/m"), proxy, "/tmp/logs")
        assert info is not None and info.foreground is True
        assert info.name == "usbserial-110" and info.pid == os.getpid()
        assert info.listen_port == proxy.port, "the port bound, not the one asked for"
        assert info.codes == {"rw": "full", "ro": "readonly"} and info.auth == "rw"
        assert info.log_dir == "/tmp/logs" and info.proxy_dir == "/tmp/m"
        mode = stat.S_IMODE(os.stat(info.path).st_mode)
        assert mode == 0o600, "the file carries the auth code"
        assert find_daemon("usbserial-110") == info
    finally:
        proxy.stop()


@posix_only
def test_a_second_serving_connect_on_the_same_stem_gets_its_own_name():
    _info("usbserial-110").write()
    proxy = _proxy({"rw": Role.FULL})
    proxy.start()
    try:
        info = register_foreground(_connect_args(), proxy, None)
        assert info.name == "usbserial-110-2"
    finally:
        proxy.stop()


def test_s39_a_serving_connect_registers_where_it_cannot_detach(monkeypatch):
    # Windows: no fork, so no `start` — but the registry works (S39).
    monkeypatch.setattr(cli, "DAEMON_SUPPORTED", False)
    proxy = _proxy({"rw": Role.FULL})
    proxy.start()
    try:
        info = register_foreground(_connect_args(), proxy, None)
        assert info is not None and info.foreground is True and info.owner == "uart-proxy"
        assert [d.name for d in list_daemons()] == [info.name]
    finally:
        proxy.stop()


@posix_only
def test_a_registry_it_cannot_write_is_a_note_not_a_failure(monkeypatch, capsys):
    def refuse(self):
        raise OSError("read-only file system")

    monkeypatch.setattr(DaemonInfo, "write", refuse)
    proxy = _proxy({"rw": Role.FULL})
    assert register_foreground(_connect_args(), proxy, None) is None
    assert "not registered" in capsys.readouterr().err


# ── what <prefix> i shows ───────────────────────────────────────────────────


def test_info_lists_address_every_code_and_the_ways_in():
    proxy = _proxy({"rw": Role.FULL, "ro": Role.READONLY})
    proxy.port = 9600
    mirrors = SimpleNamespace(stats=lambda: [SimpleNamespace(link="/tmp/m/x-0")])
    lines = session_info_lines(proxy, listen="0.0.0.0",
                               registered=_info("bench"), mirrors=mirrors,
                               log_dir="/tmp/logs")
    text = "\n".join(lines)
    assert "0.0.0.0:9600" in text
    assert "rw  (full)" in text and "ro  (readonly)" in text
    assert "uart-proxy attach bench" in text
    assert "/tmp/m/x-0" in text and "/tmp/logs" in text


def test_info_without_a_proxy_says_how_to_get_one():
    lines = session_info_lines(None, listen="")
    assert any("--serve" in line for line in lines)
    assert not any(line.startswith("auth") for line in lines)


# ── status --show-auth ──────────────────────────────────────────────────────


@posix_only
def test_status_hides_codes_unless_asked(capsys):
    _info("bench", codes={"s3cret": "full"}, foreground=True).write()
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "s3cret" not in out
    assert "--show-auth" in out
    assert "(foreground)" in out


@posix_only
def test_status_show_auth_prints_every_code_with_its_role(capsys):
    _info("bench", codes={"s3cret": "full", "look": "readonly"}).write()
    assert cli.main(["status", "--show-auth"]) == 0
    out = capsys.readouterr().out
    assert "s3cret  (full)" in out and "look  (readonly)" in out


@posix_only
def test_status_json_carries_codes_only_with_show_auth(capsys):
    _info("bench", codes={"s3cret": "full"}, foreground=True).write()
    cli.main(["status", "--json"])
    plain = json.loads(capsys.readouterr().out)["data"][0]
    assert "auth" not in plain and plain["foreground"] is True
    cli.main(["status", "--json", "--show-auth"])
    shown = json.loads(capsys.readouterr().out)["data"][0]
    assert shown["auth"] == {"s3cret": "full"}


# ── the busy hint knows a foreground holder ─────────────────────────────────


def test_a_foreground_holder_is_not_called_a_background_session():
    info = _info("bench", pid=4242, foreground=True)
    hint = describe_busy("/dev/tty.usbserial-110", holders=[], daemons=[info])
    assert "background" not in hint
    assert "in another terminal" in hint and "uart-proxy attach bench" in hint


@posix_only
def test_start_refuses_a_port_a_foreground_session_holds(capsys):
    _info("bench", foreground=True).write()
    code = cli.main(["start", "--port", "/dev/cu.usbserial-110", "--name", "other"])
    assert code == 1
    assert "already held by 'bench'" in capsys.readouterr().err


# ── the TUI: a toast, never the log ─────────────────────────────────────────


PREFIX = "ctrl+right_square_bracket"


async def _settle(pilot, tries=4):
    for _ in range(tries):
        await asyncio.sleep(0.02)
        await pilot.pause()


def _status_text(app) -> str:
    widget = app._status
    return str(getattr(widget, "content", None) or getattr(widget, "renderable", ""))


@pytest.mark.skipif(not _TEXTUAL_AVAILABLE, reason="textual not installed")
@pytest.mark.parametrize("input_mode", ["line", "char"])
def test_prefix_i_shows_the_code_in_a_toast_and_nowhere_else(input_mode):
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        session = UartSession(FakeSource(), auto_reconnect=False)
        app = UartProxyApp(session, input_mode=input_mode,
                           info=["proxy    0.0.0.0:9600", "auth     s3cret  (full)"],
                           serve_hint="0.0.0.0:9600")
        notices: list[str] = []
        session.bus.subscribe(
            lambda e: notices.append(e.text) if e.kind is EventKind.NOTICE else None)
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("i")
            await _settle(pilot)
            toasts = [n.message for n in app._notifications]
            assert len([t for t in toasts if "s3cret" in t]) == 1
            # Not in the log (what Ctrl+W copies), not on the bus (what proxy
            # clients, read-only ones included, receive), not in the status bar
            # (what ends up in screenshots) — which does say where to look.
            assert not any("s3cret" in line for line in app._copy_lines)
            assert not any("s3cret" in n for n in notices)
            status = _status_text(app)
            assert "s3cret" not in status
            assert "serve 0.0.0.0:9600" in status
        session.stop()

    asyncio.run(scenario())


@pytest.mark.skipif(not _TEXTUAL_AVAILABLE, reason="textual not installed")
def test_prefix_i_without_a_proxy_explains_rather_than_shows_nothing():
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        session = UartSession(FakeSource(), auto_reconnect=False)
        app = UartProxyApp(session)
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("i")
            await _settle(pilot)
            toasts = [n.message for n in app._notifications]
            assert any("not serving" in t for t in toasts)
            assert "serve " not in _status_text(app)
        session.stop()

    asyncio.run(scenario())


@pytest.mark.skipif(not _TEXTUAL_AVAILABLE, reason="textual not installed")
def test_the_help_lists_prefix_i():
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        session = UartSession(FakeSource(), auto_reconnect=False)
        app = UartProxyApp(session)
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("question_mark")
            await _settle(pilot)
            assert any("session info" in line for line in app._copy_lines)
        session.stop()

    asyncio.run(scenario())


# ── end to end: a real `connect --serve`, looked up from another shell ──────


@posix_only
def test_a_serving_connect_can_be_found_and_joined_from_elsewhere(tmp_path, isolated_home):
    import pty

    master, slave = pty.openpty()
    device = os.ttyname(slave)
    env = dict(os.environ, HOME=str(tmp_path),  # no real config.toml
               **{daemon_mod.HOME_ENV: str(isolated_home)})
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port", device,
         "--no-tui", "--no-log", "--serve", "--listen-port", "0"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
    try:
        banner = ""
        while "Registered as" not in banner:
            line = proc.stderr.readline()
            assert line, f"exited early: {banner}"
            banner += line
        generated = re.search(r"generated code (\w+)", banner).group(1)
        assert "every interface" in banner, "a 0.0.0.0 bind must say so"

        listed = subprocess.run(
            [sys.executable, "-m", "uart_proxy", "status", "--json", "--show-auth"],
            capture_output=True, text=True, env=env, timeout=30)
        (entry,) = json.loads(listed.stdout)["data"]
        assert entry["foreground"] is True and entry["pid"] == proc.pid
        assert entry["auth"] == {generated: "full"}

        # What `attach` does with it: reach a 0.0.0.0 bind on loopback, with
        # the recorded code, no code typed by anyone.
        info = find_daemon(entry["name"])
        client = SocketSource(connect_host(info.listen_host), info.listen_port, info.auth)
        client.open()
        client.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        os.close(master)
        os.close(slave)
    assert list_daemons(include_dead=True) == [], "an ordered exit unregisters"


@posix_only
def test_a_loopback_bind_does_not_warn_about_the_network(tmp_path, isolated_home):
    import pty

    master, slave = pty.openpty()
    env = dict(os.environ, HOME=str(tmp_path),  # no real config.toml
               **{daemon_mod.HOME_ENV: str(isolated_home)})
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port", os.ttyname(slave),
         "--no-tui", "--no-log", "--serve", "--listen", "127.0.0.1",
         "--listen-port", "0", "--auth", "given"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env)
    try:
        banner = ""
        while "Registered as" not in banner:
            line = proc.stderr.readline()
            assert line, f"exited early: {banner}"
            banner += line
        assert "every interface" not in banner
        assert "generated" not in banner, "a given code is used as given"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        os.close(master)
        os.close(slave)
