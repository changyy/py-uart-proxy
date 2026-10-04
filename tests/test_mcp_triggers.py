"""S47 over MCP: watches, events, proposals — an AI tool's side of triggers."""

from __future__ import annotations

import pytest

from uart_proxy.core.daemon import register_served
from uart_proxy.core.session import UartSession
from uart_proxy.core.triggers import Triggers
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer, TriggerPolicy

from conftest import FakeSource
from test_mcp import _text, _wait_for, isolated_home, mcp  # noqa: F401  (fixtures)


@pytest.fixture
def served():
    made = []

    def _make(policy=None):
        device = FakeSource()
        session = UartSession(device)
        triggers = Triggers(session)
        server = ProxyServer(session, {"rw": Role.FULL, "ro": Role.READONLY}, host="127.0.0.1",
                             port=0, triggers=triggers, trigger_policy=policy)
        server.start()
        session.start()
        assert _wait_for(lambda: session.is_connected)
        register_served(server, name="bench", port="COM3", baud=115200, owner="uartist", title="COM3")
        made.append((session, server, triggers))
        return device, triggers

    yield _make
    for session, server, triggers in made:
        server.stop()
        session.stop()
        triggers.close()


def _call(m, tool, **arguments) -> dict:
    """tools/call — `name` is also a tool argument here, so not Mcp.tool()."""
    wait = float(arguments.get("timeout", 10)) + 10
    return m.call("tools/call", {"name": tool, "arguments": arguments}, wait)


def _tools(m) -> set[str]:
    return {t["name"] for t in m.call("tools/list")["result"]["tools"]}


def test_s47_watch_tools_are_listed_and_propose_only_with_allow_send(mcp):
    ro, rw = mcp(), mcp("--allow-send")
    ro.start()
    rw.start()
    for name in ("watch_add", "watch_list", "watch_remove", "read_events", "wait_for_event"):
        assert name in _tools(ro)
    assert "propose_rule" not in _tools(ro) and "propose_rule" in _tools(rw)


def test_s47_watch_then_wait_for_event_then_read_events_once(served, mcp):
    device, triggers = served()
    m = mcp()
    m.start()
    added = _call(m, "watch_add", pattern="panic", name="kernel panic")
    assert not added["result"]["isError"], _text(added)
    assert triggers.list()[0]["owner"]["kind"] == "ai"
    device.feed(b"Kernel panic - not syncing\n")
    got = m.tool("wait_for_event", timeout=5)
    assert not got["result"]["isError"] and "kernel panic" in _text(got)
    first = m.tool("read_events")
    assert "Kernel panic" in _text(first)
    assert "no new events" in _text(m.tool("read_events")).lower()


def test_s47_an_event_also_arrives_as_a_notification(served, mcp):
    device, _ = served()
    m = mcp()
    m.start()
    m.tool("watch_add", pattern="FAIL")
    device.feed(b"selftest FAIL\n")
    note = m.notes.get(timeout=5)
    while note["params"]["data"].get("event") is None:
        note = m.notes.get(timeout=5)
    assert note["params"]["level"] == "notice" and "FAIL" in note["params"]["data"]["line"]


def test_s47_the_fourth_watch_is_an_error_naming_the_limit(served, mcp):
    served()
    m = mcp()
    m.start()
    for i in range(3):
        assert not m.tool("watch_add", pattern=f"w{i}")["result"]["isError"]
    over = m.tool("watch_add", pattern="w3")
    assert over["result"]["isError"] and "3" in _text(over)


def test_s47_propose_rule_says_what_the_owner_decided(served, mcp):
    device, triggers = served(TriggerPolicy(propose=lambda rule, client: True))
    m = mcp("--allow-send")
    m.start()
    result = _call(m, "propose_rule", pattern="login:", send_text="root", name="auto-login")
    assert not result["result"]["isError"] and "accepted" in _text(result)
    device.feed(b"login:\n")
    assert _wait_for(lambda: device.writes)


def test_s47_propose_rule_refused_without_an_owner_who_takes_proposals(served, mcp):
    served()
    m = mcp("--allow-send")
    m.start()
    result = m.tool("propose_rule", pattern="login:", send_text="root")
    assert result["result"]["isError"] and "refused" in _text(result)
