"""S39: the session registry on every OS."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

import uart_proxy.core.daemon as mod
from uart_proxy.core.daemon import DaemonInfo, list_daemons, register_served
from uart_proxy.core.session import UartSession
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv(mod.HOME_ENV, str(home))
    return home


def _info(pid: int) -> DaemonInfo:
    return DaemonInfo(name="x", pid=pid, port="COM3", baud=115200,
                      listen_host="127.0.0.1", listen_port=9600, auth="a")


# ── liveness without signals on Windows ─────────────────────────────────────


class FakeKernel32:
    """Stands in for ctypes.windll.kernel32: which pids exist, and how."""

    STILL_ACTIVE = 259

    def __init__(self, alive=(), exited=(), denied=()):
        self.alive, self.exited, self.denied = set(alive), set(exited), set(denied)
        self.last_error = 0
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        if pid in self.denied:
            self.last_error = 5          # ERROR_ACCESS_DENIED
            return 0
        if pid in self.alive or pid in self.exited:
            return 1000 + pid
        self.last_error = 87             # ERROR_INVALID_PARAMETER: no such pid
        return 0

    def GetExitCodeProcess(self, handle, code_ref):
        pid = handle - 1000
        code_ref._obj.value = self.STILL_ACTIVE if pid in self.alive else 0
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1

    def GetLastError(self):
        return self.last_error


@pytest.fixture
def windows(monkeypatch):
    kernel = FakeKernel32(alive={4321}, exited={4322}, denied={4})
    monkeypatch.setattr(mod.sys, "platform", "win32")
    monkeypatch.setattr(mod, "_kernel32", lambda: kernel)

    def no_kill(*a, **k):
        raise AssertionError("os.kill on Windows sends CTRL_C_EVENT for signal 0")

    monkeypatch.setattr(mod.os, "kill", no_kill)
    return kernel


def test_s39_windows_liveness_asks_the_os_and_never_signals(windows):
    assert _info(4321).is_alive
    assert not _info(4322).is_alive, "exited: not STILL_ACTIVE"
    assert not _info(4323).is_alive, "no such process"
    assert _info(4).is_alive, "access denied means it exists"
    assert windows.closed == [5321, 5322], "every handle opened is closed"


def test_s39_a_non_positive_pid_is_never_alive(windows):
    assert not _info(0).is_alive


# ── registering a served session ────────────────────────────────────────────


def _served():
    session = UartSession(FakeSource())
    server = ProxyServer(session, {"rw": Role.FULL, "ro": Role.READONLY},
                         host="127.0.0.1", port=0)
    server.start()
    return session, server


def test_s39_register_served_writes_who_serves_it_and_how_to_reach_it():
    session, server = _served()
    try:
        info = register_served(server, name="COM3", port="COM3", baud=115200,
                               owner="uartist", title="COM3 · tab 2")
        assert info is not None
        assert (info.owner, info.title) == ("uartist", "COM3 · tab 2")
        assert info.listen_port == server.port and info.pid == os.getpid()
        assert info.codes == {"rw": "full", "ro": "readonly"} and info.auth == "rw"
        assert [d.name for d in list_daemons()] == ["COM3"]
        # Another process finds it, as `status` does.
        out = subprocess.run(
            [sys.executable, "-m", "uart_proxy", "status", "--json"],
            capture_output=True, text=True, encoding="utf-8", env=os.environ.copy(), timeout=30,
        )
        listed = json.loads(out.stdout)["data"]
        assert [(s["name"], s["owner"], s["title"]) for s in listed] == [("COM3", "uartist", "COM3 · tab 2")]
        info.remove()
        assert list_daemons(include_dead=True) == []
    finally:
        server.stop()


def test_s39_a_registry_that_cannot_be_written_is_a_note(monkeypatch, capsys):
    session, server = _served()

    def refuse(self):
        raise OSError("read-only file system")

    monkeypatch.setattr(DaemonInfo, "write", refuse)
    try:
        assert register_served(server, name="COM3", port="COM3", baud=9600) is None
        assert "not registered" in capsys.readouterr().err
    finally:
        server.stop()


def test_s39_an_entry_from_before_owner_and_title_still_loads(isolated_home):
    data = {"name": "old", "pid": os.getpid(), "port": "/dev/x", "baud": 9600,
            "listen_host": "127.0.0.1", "listen_port": 1, "auth": "a",
            "started_at": time.time()}
    info = DaemonInfo.from_dict(data)
    assert (info.owner, info.title) == ("uart-proxy", "")
