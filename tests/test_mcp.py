"""S42: the MCP server, driven as an AI tool would — a subprocess over pipes."""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

import pytest

import uart_proxy.core.daemon as daemon
from uart_proxy.core.daemon import register_served
from uart_proxy.core.replay import ReplayBuffer
from uart_proxy.core.session import UartSession
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


class Mcp:
    """One `uart-proxy mcp` process, and JSON-RPC over its stdin/stdout."""

    def __init__(self, *args):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uart_proxy", "mcp", *args],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=os.environ.copy(),
        )
        self.lines: "queue.Queue[bytes]" = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self._id = 0

    def _pump(self):
        for line in self.proc.stdout:
            self.lines.put(line)

    def send_raw(self, raw: bytes):
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def recv(self, timeout=10.0) -> dict:
        line = self.lines.get(timeout=timeout)
        return json.loads(line.decode("utf-8"))   # stdout is JSON-RPC, nothing else

    def call(self, method, params=None, timeout=10.0) -> dict:
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            msg["params"] = params
        self.send_raw(json.dumps(msg).encode() + b"\n")
        reply = self.recv(timeout)
        assert reply["id"] == self._id and reply["jsonrpc"] == "2.0"
        return reply

    def notify(self, method):
        self.send_raw(json.dumps({"jsonrpc": "2.0", "method": method}).encode() + b"\n")

    def start(self, version="2025-06-18"):
        reply = self.call("initialize", {"protocolVersion": version, "capabilities": {},
                                         "clientInfo": {"name": "test-agent", "version": "1"}})
        self.notify("notifications/initialized")
        return reply["result"]

    def tool(self, name, **arguments) -> dict:
        # `timeout` is the tool's own argument; wait for the answer a bit longer.
        wait = float(arguments.get("timeout", 10)) + 10
        return self.call("tools/call", {"name": name, "arguments": arguments}, wait)

    def close(self):
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@pytest.fixture
def served():
    made = []

    def _make(*, echo=False, history=b"", codes=None):
        device = FakeSource(echo=echo)
        session = UartSession(device)
        replay = ReplayBuffer(100)
        session.bus.subscribe(replay.handle)
        server = ProxyServer(session, codes or {"rw": Role.FULL, "ro": Role.READONLY},
                             host="127.0.0.1", port=0, replay=replay)
        server.start()
        session.start()
        assert _wait_for(lambda: session.is_connected)
        if history:
            device.feed(history)
            assert _wait_for(lambda: len(replay) >= history.count(b"\n"))
        info = register_served(server, name="bench", port="COM3", baud=115200,
                               owner="uartist", title="COM3")
        made.append((session, server))
        return device, session, server, info

    yield _make
    for session, server in made:
        server.stop()
        session.stop()


@pytest.fixture
def mcp():
    made = []

    def _make(*args):
        m = Mcp(*args)
        made.append(m)
        return m

    yield _make
    for m in made:
        m.close()


def _text(result) -> str:
    """A tool result's text (given the result, or the whole reply)."""
    result = result.get("result", result)
    return "\n".join(c["text"] for c in result["content"] if c["type"] == "text")


# ── the protocol ────────────────────────────────────────────────────────────


def test_s42_initialize_answers_a_known_version_and_offers_tools(mcp):
    m = mcp()
    result = m.start("2025-03-26")
    assert result["protocolVersion"] == "2025-03-26"
    assert "tools" in result["capabilities"]
    assert result["serverInfo"]["name"] == "uart-proxy"
    other = mcp()
    assert other.start("1999-01-01")["protocolVersion"] == "2025-06-18"


def test_s42_send_tools_exist_only_with_allow_send(mcp):
    read_only = mcp()
    read_only.start()
    names = {t["name"] for t in read_only.call("tools/list")["result"]["tools"]}
    assert names == {"list_sessions", "session_status", "read_new", "tail", "wait_for"}
    allowed = mcp("--allow-send")
    allowed.start()
    tools = allowed.call("tools/list")["result"]["tools"]
    assert {"send_text", "send_hex"} <= {t["name"] for t in tools}
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object" and tool["description"]


def test_s42_bad_input_gets_errors_and_the_server_carries_on(mcp):
    m = mcp()
    m.start()
    m.send_raw(b"{not json\n")
    assert m.recv()["error"]["code"] == -32700
    assert m.call("no/such/method")["error"]["code"] == -32601
    assert m.call("tools/call", {"name": "send_text", "arguments": {"text": "x"}})["error"]["code"] == -32602
    assert m.call("ping")["result"] == {}


# ── reading a shared session ────────────────────────────────────────────────


def test_s42_list_and_status_describe_the_shared_session(served, mcp):
    device, session, server, info = served()
    m = mcp()
    m.start()
    listed = m.tool("list_sessions")["result"]
    (entry,) = listed["structuredContent"]["sessions"]
    assert (entry["name"], entry["owner"], entry["title"], entry["port"]) == ("bench", "uartist", "COM3", "COM3")
    assert entry["can_send"] is False
    status = m.tool("session_status")["result"]["structuredContent"]
    assert status["access"] == "read-only" and status["connected"] is True
    assert _wait_for(lambda: any(c["client"] == "uart-proxy mcp (test-agent)" for c in server.clients()))


def test_s42_tail_read_new_and_wait_for(served, mcp):
    device, session, server, info = served(history=b"boot 1\nboot 2\n")
    m = mcp()
    m.start()
    tail = m.tool("tail", lines=5)["result"]
    assert "boot 2" in _text(tail) and " | 00:00:" in _text(tail)
    first = m.tool("read_new")["result"]["structuredContent"]
    assert [l["text"] for l in first["lines"]] == ["boot 1", "boot 2"]
    assert m.tool("read_new")["result"]["structuredContent"]["lines"] == []
    device.feed(b"READY\n")
    hit = m.tool("wait_for", pattern="READY", timeout=5)["result"]
    assert hit.get("isError") is not True and "READY" in _text(hit)
    later = m.tool("read_new")["result"]["structuredContent"]
    assert [l["text"] for l in later["lines"]] == ["READY"]
    missed = m.tool("wait_for", pattern="never", timeout=0.5)["result"]
    assert missed["isError"] is True and "never" in _text(missed)


def test_s42_a_reply_before_the_wait_still_counts(served, mcp):
    device, session, server, info = served()
    m = mcp()
    m.start()
    m.tool("read_new")
    device.feed(b"done: 42\n")
    time.sleep(0.3)                               # it came before we asked
    hit = m.tool("wait_for", pattern=r"done: \d+", timeout=2)["result"]
    assert hit.get("isError") is not True
    again = m.tool("wait_for", pattern=r"done: \d+", timeout=0.3)["result"]
    assert again["isError"] is True, "a match is not found twice"


def test_s42_results_are_bounded(served, mcp):
    device, session, server, info = served()
    m = mcp()
    m.start()
    m.tool("read_new")                            # connected before the flood
    device.feed(b"".join(f"line {i:04d}\n".encode() for i in range(400)))
    assert _wait_for(lambda: "line 0399" in _text(m.tool("tail", lines=1)["result"]), 5)
    result = m.tool("read_new")["result"]
    assert len(result["structuredContent"]["lines"]) == 200
    assert result["structuredContent"]["lines"][-1]["text"] == "line 0399"
    assert "not shown" in _text(result)


# ── sending ─────────────────────────────────────────────────────────────────


def test_s42_send_to_a_read_only_share_is_an_error(served, mcp):
    device, session, server, info = served(codes={"ro": Role.READONLY})
    m = mcp("--allow-send")
    m.start()
    result = m.tool("send_text", text="reboot")["result"]
    assert result["isError"] is True and "read-only" in _text(result)
    time.sleep(0.2)
    assert device.writes == []


def test_s42_send_with_a_full_code_reaches_the_device_and_waits(served, mcp):
    device, session, server, info = served(echo=True)
    m = mcp("--allow-send")
    m.start()
    result = m.tool("send_text", text="uname", wait_for="uname", timeout=5)["result"]
    assert result.get("isError") is not True and "uname" in _text(result)
    assert b"uname\r" in b"".join(device.writes)
    hexed = m.tool("send_hex", hex="A5 01", timeout=1)["result"]
    assert hexed.get("isError") is not True
    assert _wait_for(lambda: b"\xa5\x01" in b"".join(device.writes))
    (origin_client,) = [c["client"] for c in server.clients()]
    assert origin_client == "uart-proxy mcp (test-agent)"
