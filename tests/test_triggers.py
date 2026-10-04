"""S46: triggers — rules that watch the session and act, safely."""

from __future__ import annotations

import json
import time

import pytest

from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.session import UartSession
from uart_proxy.core.timestamp import TimestampTracker
from uart_proxy.core.triggers import Triggers, content_hash

from conftest import FakeSource


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Harness:
    """Events fed straight to a Triggers on an idle session, on a fake clock."""

    def __init__(self, **kw) -> None:
        self.device = FakeSource()
        self.session = UartSession(self.device)
        self.clock = Clock()
        self.triggers = Triggers(self.session, clock=self.clock, timer=False, **kw)
        self.tracker = TimestampTracker()
        self.fired: list[dict] = []
        self.session.bus.subscribe(
            lambda e: self.fired.append(e.meta) if e.kind == EventKind.TRIGGER else None)

    def line(self, text: str, direction: Direction = Direction.RX, meta=None) -> None:
        self.session.bus.publish(Event(kind=EventKind.LINE, direction=direction,
                                       stamp=self.tracker.stamp(), text=text,
                                       data=text.encode(), meta=meta or {}))

    def chunk(self, data: bytes) -> None:
        self.session.bus.publish(Event(kind=EventKind.DATA, direction=Direction.RX,
                                       stamp=self.tracker.stamp(), data=data,
                                       text=data.decode("latin-1")))

    def status(self, state: str) -> None:
        self.session.bus.publish(Event(kind=EventKind.STATUS, direction=Direction.SYS,
                                       stamp=self.tracker.stamp(), text=state))


def _rule(**when) -> dict:
    return {"name": "r", "when": when, "actions": [{"kind": "event"}]}


# ── matching ────────────────────────────────────────────────────────────────


def test_s46_a_text_rule_fires_for_a_matching_rx_line_only():
    h = Harness()
    h.triggers.add(_rule(text="panic"))
    h.line("all good")
    h.line("Kernel panic - not syncing")
    h.line("panic", Direction.TX)                       # rx only by default
    assert [e["line"] for e in h.fired] == ["Kernel panic - not syncing"]


def test_s46_case_insensitive_by_default_and_sensitive_when_asked():
    h = Harness()
    h.triggers.add(_rule(text="error"))
    h.triggers.add({**_rule(text="error", case=True), "name": "strict"})
    h.line("ERROR: disk")
    assert [e["name"] for e in h.fired] == ["r"]


def test_s46_regex_groups_reach_the_event():
    h = Harness()
    h.triggers.add(_rule(regex=r"temp=(\d+)C"))
    h.line("sensor temp=81C")
    assert h.fired[0]["groups"] == ["81"]


def test_s46_a_prompt_flushed_without_newline_is_a_line_too():
    """S3's idle flush makes `login: ` a line: a text rule needs nothing more."""
    device = FakeSource()
    session = UartSession(device)
    triggers = Triggers(session)
    triggers.add(_rule(text="login:"))
    session.start()
    try:
        assert _wait_for(lambda: session.is_connected)
        device.feed(b"\r\nlogin: ")
        assert _wait_for(lambda: triggers.events()), "fired on the flushed prompt"
    finally:
        session.stop()
        triggers.close()


def test_s46_hex_matches_a_sequence_split_across_chunks():
    h = Harness()
    h.triggers.add(_rule(hex="5A 03 30"))
    h.chunk(b"\x00\x5a")
    h.chunk(b"\x03\x30\x01")
    assert len(h.fired) == 1 and h.fired[0]["line"] == "5A 03 30"


def test_s46_silence_fires_once_and_again_only_after_new_output():
    h = Harness()
    h.triggers.add(_rule(silence=5))
    h.status("connected")
    h.line("boot")
    h.clock.now += 4.9
    h.triggers.tick()
    assert not h.fired
    h.clock.now += 0.2
    h.triggers.tick()
    h.clock.now += 10
    h.triggers.tick()
    assert len(h.fired) == 1
    h.line("more")
    h.clock.now += 5.1
    h.triggers.tick()
    assert len(h.fired) == 2


def test_s46_state_fires_on_a_drop_and_on_the_return():
    h = Harness()
    h.triggers.add({**_rule(state="disconnected"), "name": "drop"})
    h.triggers.add({**_rule(state="reconnected"), "name": "back"})
    h.status("connected")
    h.status("error")
    h.status("reconnecting")        # the same drop: once
    h.status("connected")
    assert [e["name"] for e in h.fired] == ["drop", "back"]


# ── limits ──────────────────────────────────────────────────────────────────


def test_s46_once_fires_a_single_time():
    h = Harness()
    h.triggers.add({**_rule(text="x"), "limit": {"once": True}})
    for _ in range(3):
        h.line("x")
        h.clock.now += 5
    assert len(h.fired) == 1


def test_s46_cooldown_skips_matches_inside_it():
    h = Harness()
    h.triggers.add({**_rule(text="x"), "limit": {"cooldown": 10}})
    h.line("x")
    h.clock.now += 5
    h.line("x")
    h.clock.now += 6
    h.line("x")
    assert len(h.fired) == 2


def test_s46_after_n_within_a_window():
    h = Harness()
    h.triggers.add({**_rule(text="retry"), "limit": {"after": 3, "window": 10, "cooldown": 0}})
    h.line("retry")
    h.clock.now += 1
    h.line("retry")
    assert not h.fired
    h.clock.now += 1
    h.line("retry")
    assert len(h.fired) == 1
    h.clock.now += 20                                  # the window passed: count again
    h.line("retry")
    assert len(h.fired) == 1


def test_s46_a_rule_past_max_per_minute_is_disabled_with_its_reason():
    h = Harness()
    rid = h.triggers.add({**_rule(text="x"), "limit": {"cooldown": 0, "max_per_minute": 5}})
    for _ in range(20):
        h.line("x")
    assert len(h.fired) == 5
    rule = next(r for r in h.triggers.list() if r["id"] == rid)
    assert rule["enabled"] is False and "5" in rule["disabled_reason"]


# ── refusing what is unsafe ─────────────────────────────────────────────────


@pytest.mark.parametrize("pattern", [r"(a+)+$", r"(.*)*x", r"(\w+\s?)*$", "x" * 600, "(unclosed"])
def test_s46_unsafe_or_invalid_patterns_are_refused(pattern):
    h = Harness()
    with pytest.raises(ValueError):
        h.triggers.add(_rule(regex=pattern))


@pytest.mark.parametrize("text", ["user {1}", r"\1", r"\g<name>"])
def test_s46_nothing_from_the_match_may_be_sent(text):
    h = Harness()
    with pytest.raises(ValueError, match="match"):
        h.triggers.add({**_rule(text="login:"), "actions": [{"kind": "send", "text": text}]})


@pytest.mark.parametrize("kind", ["run", "exec", "webhook", "shell"])
def test_s46_no_action_runs_a_program_or_reaches_the_network(kind):
    h = Harness()
    with pytest.raises(ValueError):
        h.triggers.add({**_rule(text="x"), "actions": [{"kind": kind, "command": "rm -rf /"}]})


def test_s46_a_huge_line_does_not_stall_matching():
    h = Harness()
    h.triggers.add(_rule(regex=r"a.*b.*c"))
    start = time.monotonic()
    h.line("a" * 1_000_000)
    assert time.monotonic() - start < 1.0 and not h.fired


# ── sending ─────────────────────────────────────────────────────────────────


def _live(writable=True, echo=False):
    device = FakeSource(writable=writable, echo=echo)
    session = UartSession(device)
    triggers = Triggers(session)
    notices = []
    session.bus.subscribe(lambda e: notices.append(e.text) if e.kind == EventKind.NOTICE else None)
    tx = []
    session.bus.subscribe(lambda e: tx.append(e) if e.kind == EventKind.LINE and e.direction == Direction.TX else None)
    session.start()
    assert _wait_for(lambda: session.is_connected)
    return device, session, triggers, notices, tx


def _send_rule(text="root"):
    return {"name": "auto-login", "when": {"text": "login:"},
            "actions": [{"kind": "send", "text": text, "eol": "cr"}]}


def test_s46_an_approved_send_rule_writes_with_a_rule_origin():
    device, session, triggers, _, tx = _live()
    try:
        rid = triggers.add(_send_rule())
        triggers.approve(rid)
        device.feed(b"login:\n")
        assert _wait_for(lambda: device.writes)
        assert device.writes[0] == b"root\r"
        assert _wait_for(lambda: tx)
        origin = tx[0].meta["origin"]
        assert origin["via"] == "rule" and origin["rule"] == rid and origin["owner"]["kind"] == "person"
    finally:
        session.stop()
        triggers.close()


def test_s46_an_unapproved_or_edited_send_rule_does_not_act():
    device, session, triggers, _, _ = _live()
    try:
        rid = triggers.add(_send_rule())                  # never approved
        device.feed(b"login:\n")
        assert _wait_for(lambda: triggers.events())
        assert triggers.events()[0]["actions"][-1]["ok"] is False
        triggers.approve(rid)
        triggers.update(rid, _send_rule("admin"))          # edited after approval
        device.feed(b"login:\n")
        assert _wait_for(lambda: len(triggers.events()) == 2)
        assert not device.writes
    finally:
        session.stop()
        triggers.close()


def test_s46_a_send_rule_does_not_fire_on_the_echo_of_what_it_sent():
    device, session, triggers, _, _ = _live(echo=True)
    try:
        rid = triggers.add({"name": "pong", "when": {"text": "ping"},
                            "limit": {"cooldown": 0},
                            "actions": [{"kind": "send", "text": "ping", "eol": "lf"}]})
        triggers.approve(rid)
        device.feed(b"ping\n")
        assert _wait_for(lambda: device.writes)
        time.sleep(0.5)
        assert len(device.writes) == 1, "its own echo did not set it off again"
    finally:
        session.stop()
        triggers.close()


def test_s46_on_a_session_that_cannot_be_written_a_send_is_skipped_with_a_notice():
    device, session, triggers, notices, _ = _live(writable=False)
    try:
        triggers.approve(triggers.add(_send_rule()))
        device.feed(b"login:\n")
        assert _wait_for(lambda: any("auto-login" in n for n in notices))
        assert not device.writes
    finally:
        session.stop()
        triggers.close()


# ── events ──────────────────────────────────────────────────────────────────


def test_s46_events_are_numbered_with_context_and_kept(tmp_path):
    path = tmp_path / "s-events.jsonl"
    h = Harness(events_path=str(path))
    h.triggers.add({**_rule(text="FAIL"), "context": 2})
    for text in ("one", "two", "three", "test FAIL"):
        h.line(text)
    events = h.triggers.events()
    assert events[0]["seq"] == 1 and events[0]["context"] == ["two", "three"]
    assert h.triggers.events(since=1) == []
    row = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert row["line"] == "test FAIL" and row["name"] == "r"


def test_s46_rules_round_trip_through_a_file_and_imported_send_rules_arrive_off(tmp_path):
    h = Harness()
    h.triggers.add({**_rule(text="x"), "name": "watch"})
    h.triggers.approve(h.triggers.add(_send_rule()))
    path = tmp_path / "rules.json"
    h.triggers.dump(str(path))
    other = Harness()
    other.triggers.load(str(path))
    rules = {r["name"]: r for r in other.triggers.list()}
    assert rules["watch"]["enabled"] is True
    assert rules["auto-login"]["enabled"] is False and rules["auto-login"]["approved"] is None
    assert rules["auto-login"]["level"] == 1 and rules["watch"]["level"] == 0


def test_s46_content_hash_covers_what_the_rule_does():
    a = _send_rule("root")
    assert content_hash(a) == content_hash({**a, "name": "renamed"})
    assert content_hash(a) != content_hash(_send_rule("admin"))


# ── the CLI ─────────────────────────────────────────────────────────────────


def test_s46_cli_rules_load_and_send_rules_wait_for_approve_rules(tmp_path, capsys):
    from uart_proxy import cli

    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"rules": [
        {"name": "panic", "when": {"text": "panic"}, "actions": [{"kind": "notify"}]},
        {"name": "auto-login", "when": {"text": "login:"},
         "actions": [{"kind": "send", "text": "root"}]}]}), encoding="utf-8")
    parser = cli.build_parser()
    for flags, sending_on in (([], False), (["--approve-rules"], True)):
        args = parser.parse_args(["connect", "--port", "/dev/null", "--rules", str(rules), *flags])
        session = UartSession(FakeSource())
        triggers = cli._build_triggers(session, args, None)
        try:
            by_name = {r["name"]: r for r in triggers.list()}
            assert by_name["panic"]["enabled"] is True
            assert by_name["auto-login"]["enabled"] is sending_on
            assert (by_name["auto-login"]["approved"] is not None) is sending_on
        finally:
            triggers.close()
    assert "--approve-rules" in capsys.readouterr().err

