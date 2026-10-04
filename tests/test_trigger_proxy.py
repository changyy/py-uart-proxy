"""S47: triggers through the proxy — watches, proposals, the owner's policy."""

from __future__ import annotations

import threading
import time

import pytest

from uart_proxy.client import SessionClient, SessionClientError
from uart_proxy.core.session import UartSession
from uart_proxy.core.triggers import Triggers
from uart_proxy.proxy.protocol import Role
from uart_proxy.proxy.server import ProxyServer, TriggerPolicy

from conftest import FakeSource


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Served:
    def __init__(self, policy=None, with_triggers=True):
        self.device = FakeSource()
        self.session = UartSession(self.device)
        self.triggers = Triggers(self.session) if with_triggers else None
        self.server = ProxyServer(self.session, {"rw": Role.FULL, "ro": Role.READONLY},
                                  host="127.0.0.1", port=0, triggers=self.triggers,
                                  trigger_policy=policy)
        self.server.start()
        self.session.start()
        assert _wait_for(lambda: self.session.is_connected)
        self.clients = []

    def client(self, code="ro", name="claude-ai"):
        c = SessionClient("127.0.0.1", self.server.port, code, client_name=name, replay=0)
        c.connect()
        self.clients.append(c)
        return c

    def close(self):
        for c in self.clients:
            c.close()
        self.server.stop()
        self.session.stop()
        if self.triggers:
            self.triggers.close()


@pytest.fixture
def served():
    s = Served()
    yield s
    s.close()


def test_s47_a_read_only_client_watches_and_hears_the_event(served):
    c = served.client("ro")
    wid = c.watch_add({"text": "panic"}, name="kernel panic")
    rules = served.triggers.list()
    assert rules[0]["id"] == wid and rules[0]["owner"]["kind"] == "ai"
    assert rules[0]["owner"]["client"] == "claude-ai" and rules[0]["level"] == 0
    served.device.feed(b"Kernel panic - not syncing\n")
    event = c.wait_event(timeout=3)
    assert event is not None and event["name"] == "kernel panic" and "panic" in event["line"]


def test_s47_the_fourth_watch_is_refused_at_the_default_limit(served):
    c = served.client("ro")
    for i in range(3):
        c.watch_add({"text": f"w{i}"})
    with pytest.raises(SessionClientError, match="3"):
        c.watch_add({"text": "w3"})
    assert len(c.watch_list()) == 3


def test_s47_a_client_lists_and_removes_only_its_own_watches(served):
    a, b = served.client("ro", "a"), served.client("ro", "b")
    wa = a.watch_add({"text": "x"})
    b.watch_add({"text": "y"})
    assert [w["id"] for w in a.watch_list()] == [wa]
    with pytest.raises(SessionClientError):
        b.watch_remove(wa)
    a.watch_remove(wa)
    assert [r["owner"]["client"] for r in served.triggers.list()] == ["b"]


def test_s47_disconnecting_removes_the_clients_watches(served):
    c = served.client("ro")
    c.watch_add({"text": "x"})
    c.close()
    assert _wait_for(lambda: not served.triggers.list())


def test_s47_a_watch_with_actions_is_refused(served):
    c = served.client("rw")
    with pytest.raises(SessionClientError, match="only"):
        c._request({"type": "watch_add", "when": {"text": "x"},
                    "actions": [{"kind": "send", "text": "y"}]}, "watch_ok")


def test_s47_proposals_are_refused_read_only_or_with_no_one_to_ask(served):
    rule = {"name": "auto", "when": {"text": "login:"}, "actions": [{"kind": "send", "text": "root"}]}
    assert served.client("ro").propose_rule(rule)["status"] == "refused"
    assert served.client("rw").propose_rule(rule)["status"] == "refused"   # no propose callable
    assert served.triggers.list() == []


def test_s47_an_accepted_proposal_becomes_an_ai_rule_that_sends():
    asked = []

    def propose(rule, client):
        asked.append((rule, client))
        return True

    s = Served(TriggerPolicy(propose=propose))
    try:
        c = s.client("rw")
        result = c.propose_rule({"name": "auto", "when": {"text": "login:"},
                                 "actions": [{"kind": "send", "text": "root", "eol": "cr"}]})
        assert result["status"] == "accepted" and asked[0][1]["client"] == "claude-ai"
        assert "root" in str(asked[0][0]["actions"])
        rule = s.triggers.get(result["rule"])
        assert rule["owner"]["kind"] == "ai" and rule["approved"]
        s.device.feed(b"login:\n")
        assert _wait_for(lambda: s.device.writes)
        assert s.device.writes[0] == b"root\r"
    finally:
        s.close()


def test_s47_a_declined_proposal_adds_nothing_and_pings_carry_on_while_asked():
    gate = threading.Event()

    def propose(rule, client):
        gate.wait(2)
        return False

    s = Served(TriggerPolicy(propose=propose))
    try:
        c = s.client("rw")
        result = {}
        t = threading.Thread(target=lambda: result.update(c.propose_rule(
            {"name": "x", "when": {"text": "x"}, "actions": [{"kind": "send", "text": "y"}]}, timeout=5)))
        t.start()
        time.sleep(0.3)
        assert c.watch_list() == [], "the connection still answers while the owner is asked"
        gate.set()
        t.join(5)
        assert result["status"] == "declined" and s.triggers.list() == []
    finally:
        s.close()


def test_s47_stopping_the_share_removes_ai_rules_but_keeps_the_persons():
    s = Served(TriggerPolicy(max_watches=5))
    try:
        s.triggers.add({"name": "mine", "when": {"text": "x"}})
        s.client("ro").watch_add({"text": "y"})
        s.server.stop()
        assert [r["name"] for r in s.triggers.list()] == ["mine"]
    finally:
        s.close()


def test_s47_with_max_watches_zero_or_no_triggers_watches_are_refused():
    for s in (Served(TriggerPolicy(max_watches=0)), Served(with_triggers=False)):
        try:
            with pytest.raises(SessionClientError):
                s.client("ro").watch_add({"text": "x"})
        finally:
            s.close()


def test_s47_events_reach_every_client_and_read_events_returns_each_once(served):
    served.triggers.add({"name": "mine", "when": {"text": "boot"}})
    c = served.client("ro")
    served.device.feed(b"boot ok\n")
    assert _wait_for(lambda: c.events())
    first = c.events()
    assert first[0]["name"] == "mine"
    assert c.events(since=first[-1]["seq"]) == []
