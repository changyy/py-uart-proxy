"""S44: health through MCP — status, verdicts on results, waiting for the device, notifications."""

from __future__ import annotations

import queue
import time

from test_mcp import _text, _wait_for, isolated_home, mcp, served  # noqa: F401  (fixtures)


def _note(m, timeout=10.0, level=None):
    """The next notifications/message (optionally of a level)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            note = m.notes.get(timeout=max(0.05, deadline - time.monotonic()))
        except queue.Empty:
            break
        if note["method"] == "notifications/message" and (level is None or note["params"]["level"] == level):
            return note
    raise AssertionError(f"no {level or ''} notification within {timeout}s")


def test_s44_an_absent_device_is_down_with_replug_advice(served, mcp):
    device, session, server, info = served(absent=True)
    assert _wait_for(lambda: session.device_health()["state"] == "waiting")
    m = mcp()
    m.start()
    status = m.tool("session_status")["result"]["structuredContent"]
    assert status["health"] == "down" and status["device"]["state"] == "waiting"
    assert status["share"]["connected"] is True and "re-plug" in status["advice"]
    tail = m.tool("tail")["result"]
    assert tail["structuredContent"]["health"]["level"] == "down", "every result says when it is not ok"
    assert "re-plug" in _text(tail)


def test_s44_wait_for_device_returns_when_it_comes_back(served, mcp):
    device, session, server, info = served(absent=True)
    assert _wait_for(lambda: session.device_health()["state"] == "waiting")
    m = mcp()
    m.start()
    missed = m.tool("wait_for_device", timeout=0.5)["result"]
    assert missed["isError"] is True and "re-plug" in _text(missed)
    device.fail_opens = 0                          # plugged back in
    back = m.tool("wait_for_device", timeout=10)["result"]
    assert back.get("isError") is not True
    assert back["structuredContent"]["health"] == "ok"


def test_s44_a_wait_for_that_times_out_says_whether_anyone_was_there(served, mcp):
    device, session, server, info = served(absent=True)
    assert _wait_for(lambda: session.device_health()["state"] == "waiting")
    m = mcp()
    m.start()
    result = m.tool("wait_for", pattern="login:", timeout=0.3)["result"]
    assert result["isError"] is True and "not there" in _text(result)


def test_s44_a_drop_and_return_are_notified(served, mcp):
    device, session, server, info = served()
    m = mcp(env={"UART_PROXY_HEALTH_SETTLE": "1.5"})
    m.start()
    m.tool("session_status")                        # attaches: notifications start
    device.drop()
    warning = _note(m, level="warning")
    assert warning["params"]["data"]["session"] == "bench"
    assert warning["params"]["data"]["health"] == "down" and warning["params"]["data"]["advice"]
    notice = _note(m, level="notice")
    assert notice["params"]["data"]["health"] == "degraded"
    info = _note(m, level="info")
    assert info["params"]["data"]["health"] == "ok"


def test_s44_logging_set_level_is_accepted(mcp):
    m = mcp()
    result = m.start()
    assert "logging" in result["capabilities"]
    assert m.call("logging/setLevel", {"level": "warning"})["result"] == {}


def test_s44_a_share_that_stopped_says_share_again(served, mcp):
    device, session, server, info = served()
    m = mcp()
    m.start()
    assert m.tool("session_status")["result"]["structuredContent"]["health"] == "ok"
    server.stop()
    info.remove()
    result = m.tool("session_status")["result"]
    assert result["isError"] is True and "share" in _text(result).lower()
