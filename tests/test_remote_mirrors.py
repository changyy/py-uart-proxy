"""S33: a remote (or attached) stream as local PTY mirrors."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import pytest

from uart_proxy import cli
from uart_proxy.cli import _maybe_build_pty_proxy
from uart_proxy.core.pty_proxy import PTY_SUPPORTED
from uart_proxy.core.session import UartSession
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer

from conftest import FakeSource

pytestmark = pytest.mark.skipif(not PTY_SUPPORTED, reason="POSIX pty")


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# ── flags and names ─────────────────────────────────────────────────────────


def test_remote_and_attach_take_the_mirror_flags():
    parser = cli.build_parser()
    rem = parser.parse_args(["remote", "--host", "h", "--auth", "x",
                             "--proxy-dir", "/tmp/m", "--proxy-count", "3",
                             "--mirror-name", "bench"])
    assert (rem.proxy_dir, rem.proxy_count, rem.mirror_stem) == ("/tmp/m", 3, "bench")
    att = parser.parse_args(["attach", "--proxy-dir"])
    assert att.proxy_dir is not None and att.mirror_stem is None


def test_mirrors_stay_off_unless_asked():
    args = cli.build_parser().parse_args(["remote", "--host", "h", "--auth", "x"])
    assert _maybe_build_pty_proxy(UartSession(FakeSource()), args) is None


def test_a_mirror_stem_names_the_links(tmp_path):
    args = argparse.Namespace(proxy_dir=str(tmp_path), proxy=None, proxy_count=2,
                              tx_merge="raw", mirror_stem="10.0.0.5-9600",
                              port=9600, name=None)
    group = _maybe_build_pty_proxy(UartSession(FakeSource()), args)
    group.start()
    try:
        assert [os.path.basename(m.link) for m in group.stats()] == \
            ["10.0.0.5-9600-0", "10.0.0.5-9600-1"]
    finally:
        group.stop()


# ── end to end ──────────────────────────────────────────────────────────────


@pytest.fixture
def server():
    device = FakeSource()
    session = UartSession(device, default_eol=b"\r")
    srv = ProxyServer(session, {"rw": Role.FULL, "ro": Role.READONLY},
                      host="127.0.0.1", port=0)
    srv.start()
    session.start()
    yield device, srv
    srv.stop()
    session.stop()


def _remote(srv, tmp_path, code):
    mirrors = tmp_path / "mirrors"
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "remote", "--host", "127.0.0.1",
         "--port", str(srv.port), "--auth", code, "--no-tui", "--no-log",
         "--proxy-dir", str(mirrors), "--proxy-count", "1"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=dict(os.environ, HOME=str(tmp_path)))
    link = mirrors / f"127.0.0.1-{srv.port}-0"
    assert _wait_for(link.exists), proc.stderr.read1(4096) if proc.poll() else "no link"
    return proc, link


def _open_raw(path):
    import tty

    fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
    tty.setraw(fd)
    return fd


def _read_until(fd, needle: bytes, timeout=10.0) -> bytes:
    import select

    got = b""
    deadline = time.monotonic() + timeout
    while needle not in got and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            got += os.read(fd, 4096)
    return got


def test_a_remote_port_becomes_a_local_pty(server, tmp_path):
    device, srv = server
    proc, link = _remote(srv, tmp_path, "rw")
    fd = _open_raw(link)
    try:
        assert _wait_for(lambda: srv.client_count == 1)
        time.sleep(0.3)
        device.feed(b"login: ")
        assert b"login: " in _read_until(fd, b"login: "), "RX reaches the mirror"
        os.write(fd, b"root\r")
        assert _wait_for(lambda: b"".join(device.writes) == b"root\r"), \
            "what is typed into the mirror reaches the far device"
    finally:
        os.close(fd)
        proc.terminate()
        proc.communicate(timeout=10)
    assert not link.exists(), "the mirror link goes with the client"


def test_a_readonly_code_gives_a_read_only_mirror(server, tmp_path):
    device, srv = server
    proc, link = _remote(srv, tmp_path, "ro")
    fd = _open_raw(link)
    try:
        assert _wait_for(lambda: srv.client_count == 1)
        time.sleep(0.3)
        device.feed(b"still visible\r\n")
        assert b"still visible" in _read_until(fd, b"still visible")
        os.write(fd, b"reboot\r")
        time.sleep(0.6)
        assert device.writes == [], "a read-only code must not write through a mirror"
    finally:
        os.close(fd)
        proc.terminate()
        out, _ = proc.communicate(timeout=10)
    # Headless mode prints notices on stdout.
    assert "read-only" in out.decode(), "the refusal is said, not silent"
