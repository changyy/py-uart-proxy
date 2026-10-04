"""
Triggers: when the device says X, do Y (SPEC S46).

Rules are data, never code. Each watches the session — a line (text or a
regular expression), a byte sequence, a silence, the device's state — and,
within its limits, makes an event and acts. Actions are ranked by risk:

* level 0 — ``event`` (always), ``mark``, ``notify``: they only tell;
* level 1 — ``send``: literal text or bytes to the device, only once a person
  approved exactly that rule (``approve``), and never anything taken from the
  match — what a device prints never becomes what it is told.

Nothing here runs a program or reaches the network.

``Triggers`` subscribes to the session's bus. Events are published back on it
(``EventKind.TRIGGER``, the event as ``meta``), kept in a ring
(``events(since)``) and appended to ``events_path`` when given.
"""

from __future__ import annotations

import collections
import copy
import hashlib
import itertools
import json
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from .events import Direction, Event, EventKind

if TYPE_CHECKING:  # avoid a circular import; only needed for type hints
    from .session import UartSession

logger = logging.getLogger(__name__)

MAX_PATTERN = 512          # characters in a regular expression
MATCH_SPAN = 4096          # characters of a line a pattern looks at
MAX_CONTEXT = 20
EVENT_RING = 500
ECHO_WINDOW = 2.0          # seconds a send rule ignores its own echo
TICK = 0.2                 # seconds between silence checks

LEVEL0 = {"event", "mark", "notify"}
LEVEL1 = {"send"}
EOLS = {"cr": b"\r", "lf": b"\n", "crlf": b"\r\n", "none": b""}
DEFAULT_LIMIT = {"once": False, "cooldown": 1.0, "max_per_minute": 30, "after": 1, "window": 60.0}
WHEN_KINDS = ("text", "regex", "hex", "silence", "state")
STATES = ("connected", "disconnected", "reconnected")
# Something from the match put into what is sent: a template slot or a
# back-reference.
_FROM_MATCH = re.compile(r"\{\d+\}|\\\d|\\g<")


def _sre():
    try:
        from re import _parser as parser          # Python 3.11+
        from re import _constants as constants
    except ImportError:                            # 3.10
        import sre_constants as constants          # type: ignore[no-redef]
        import sre_parse as parser                 # type: ignore[no-redef]
    return parser, constants


def _nested_repeat(pattern: str) -> bool:
    """True when an unbounded repeat holds another repeat: ``(a+)+``, ``(.*)*``.

    Python's ``re`` has no timeout; such a pattern can take exponential time
    on a line that almost matches, stalling the session's read thread.
    """
    parser, c = _sre()
    repeats = (c.MAX_REPEAT, c.MIN_REPEAT) + ((c.POSSESSIVE_REPEAT,) if hasattr(c, "POSSESSIVE_REPEAT") else ())

    def walk(items, inside_unbounded: bool) -> bool:
        for op, av in items:
            if op in repeats:
                low, high, sub = av
                unbounded = high == c.MAXREPEAT or high > 100
                if inside_unbounded and high > 1:
                    return True
                if walk(sub, inside_unbounded or unbounded):
                    return True
            elif op == c.SUBPATTERN:
                if walk(av[-1], inside_unbounded):
                    return True
            elif op == c.BRANCH:
                if any(walk(alt, inside_unbounded) for alt in av[1]):
                    return True
            elif op in (c.ASSERT, c.ASSERT_NOT):
                if walk(av[1], inside_unbounded):
                    return True
        return False

    return walk(parser.parse(pattern), False)


def _hex_bytes(text: str) -> bytes:
    cleaned = re.sub(r"[\s,:]|0x", "", text, flags=re.IGNORECASE)
    if not cleaned or len(cleaned) % 2 or not re.fullmatch(r"[0-9a-fA-F]+", cleaned):
        raise ValueError(f"not hex bytes: {text!r}")
    return bytes.fromhex(cleaned)


def level_of(actions: list[dict]) -> int:
    return 1 if any(a.get("kind") in LEVEL1 for a in actions) else 0


def normalise(rule: dict) -> dict:
    """A rule checked and filled with defaults; ``ValueError`` names what is wrong."""
    if not isinstance(rule, dict):
        raise ValueError("a rule is an object")
    when = dict(rule.get("when") or {})
    kinds = [k for k in WHEN_KINDS if k in when]
    if len(kinds) != 1:
        raise ValueError(f"'when' needs exactly one of {', '.join(WHEN_KINDS)}")
    kind = kinds[0]
    out_when: dict[str, Any] = {kind: when[kind]}
    if kind in ("text", "regex"):
        pattern = when[kind]
        if not isinstance(pattern, str) or not pattern:
            raise ValueError(f"'{kind}' must be non-empty text")
        if kind == "regex":
            if len(pattern) > MAX_PATTERN:
                raise ValueError(f"the regex is longer than {MAX_PATTERN} characters")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"the regex does not compile: {exc}") from None
            if _nested_repeat(pattern):
                raise ValueError("the regex nests repetition (like (a+)+), which can stall on a long line")
        out_when["case"] = bool(when.get("case", False))
        direction = when.get("direction", "rx")
        if direction not in ("rx", "tx"):
            raise ValueError("'direction' is rx or tx")
        out_when["direction"] = direction
    elif kind == "hex":
        out_when["hex"] = _hex_bytes(str(when["hex"])).hex(" ").upper()
    elif kind == "silence":
        seconds = when["silence"]
        if not isinstance(seconds, (int, float)) or not 0.5 <= seconds <= 86400:
            raise ValueError("'silence' is seconds, from 0.5 to 86400")
        out_when["silence"] = float(seconds)
    elif kind == "state":
        if when["state"] not in STATES:
            raise ValueError(f"'state' is one of {', '.join(STATES)}")

    limit = {**DEFAULT_LIMIT, **(rule.get("limit") or {})}
    for key in ("cooldown", "window"):
        if not isinstance(limit[key], (int, float)) or limit[key] < 0:
            raise ValueError(f"'{key}' is seconds, 0 or more")
    for key in ("max_per_minute", "after"):
        if not isinstance(limit[key], int) or limit[key] < 1:
            raise ValueError(f"'{key}' is a whole number, 1 or more")
    limit["once"] = bool(limit["once"])

    actions = rule.get("actions") or [{"kind": "event"}]
    out_actions = []
    for action in actions:
        kind_a = (action or {}).get("kind")
        if kind_a in LEVEL0:
            out_actions.append({"kind": kind_a})
        elif kind_a == "send":
            if ("text" in action) == ("hex" in action):
                raise ValueError("a send has 'text' or 'hex'")
            if "text" in action:
                text = str(action["text"])
                if _FROM_MATCH.search(text):
                    raise ValueError("a send cannot use anything from the match ({1}, \\1, \\g<…>): "
                                     "what a device prints never becomes what it is told")
                eol = action.get("eol", "cr")
                if eol not in EOLS:
                    raise ValueError(f"'eol' is one of {', '.join(EOLS)}")
                out_actions.append({"kind": "send", "text": text, "eol": eol})
            else:
                out_actions.append({"kind": "send", "hex": _hex_bytes(str(action["hex"])).hex(" ").upper()})
        else:
            raise ValueError(f"unknown action {kind_a!r}: actions are event, mark, notify and send "
                             "— none runs a program or reaches the network")
    if not any(a["kind"] == "event" for a in out_actions):
        out_actions.insert(0, {"kind": "event"})

    context = rule.get("context", 3)
    if not isinstance(context, int) or not 0 <= context <= MAX_CONTEXT:
        raise ValueError(f"'context' is 0 to {MAX_CONTEXT} lines")
    owner = rule.get("owner") or {"kind": "person"}
    if owner.get("kind") not in ("person", "ai"):
        raise ValueError("'owner' is a person or an ai")
    return {
        "name": str(rule.get("name") or "rule")[:80],
        "owner": owner,
        "enabled": bool(rule.get("enabled", True)),
        "when": out_when,
        "limit": limit,
        "context": context,
        "actions": out_actions,
        "only_port": rule.get("only_port"),
        "approved": rule.get("approved"),
    }


def content_hash(rule: dict) -> str:
    """What a person approves: what the rule matches, its limits and its actions."""
    norm = normalise({**rule, "approved": None})
    body = {"when": norm["when"], "limit": norm["limit"], "actions": norm["actions"]}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


class _Rule:
    def __init__(self, rid: str, spec: dict) -> None:
        self.id = rid
        self.spec = spec
        self.disabled_reason: Optional[str] = None
        self.fired = 0
        self.last_fire: Optional[float] = None
        self.fire_times: collections.deque = collections.deque()
        self.match_times: collections.deque = collections.deque()
        self.sent: list[tuple[str, float]] = []        # (text sent, when): its echo is ignored
        when = spec["when"]
        self.kind = next(k for k in WHEN_KINDS if k in when)
        self.regex: Optional[re.Pattern] = None
        if self.kind == "regex":
            self.regex = re.compile(when["regex"], 0 if when["case"] else re.IGNORECASE)
        elif self.kind == "text":
            flags = 0 if when["case"] else re.IGNORECASE
            self.regex = re.compile(re.escape(when["text"]), flags)
        self.hex = bytes.fromhex(when["hex"]) if self.kind == "hex" else b""
        self.silent_fired = False

    @property
    def level(self) -> int:
        return level_of(self.spec["actions"])

    def view(self) -> dict:
        return {"id": self.id, **copy.deepcopy(self.spec), "level": self.level,
                "fired": self.fired, "disabled_reason": self.disabled_reason}


class Triggers:
    def __init__(self, session: "UartSession", *, events_path: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic, timer: bool = True) -> None:
        self.session = session
        self.events_path = events_path
        self._clock = clock
        self._lock = threading.RLock()
        self._rules: dict[str, _Rule] = {}
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._events: collections.deque = collections.deque(maxlen=EVENT_RING)
        self._context: collections.deque = collections.deque(maxlen=MAX_CONTEXT)
        self._hex_tail = b""
        self._last_rx = clock()
        self._connected = False
        self._ever_connected = False
        self._dropped = False
        self._unsubscribe = session.bus.subscribe(self._on_event)
        self._stop = threading.Event()
        self._timer: Optional[threading.Thread] = None
        if timer:
            self._timer = threading.Thread(target=self._tick_loop, name="uart-triggers", daemon=True)
            self._timer.start()

    # ── rules ────────────────────────────────────────────────────────────────

    def add(self, rule: dict) -> str:
        spec = normalise(rule)
        with self._lock:
            rid = str(rule.get("id") or f"r{next(self._ids)}")
            while rid in self._rules:
                rid = f"r{next(self._ids)}"
            self._rules[rid] = _Rule(rid, spec)
        return rid

    def update(self, rid: str, rule: dict) -> None:
        """Replace a rule's content, keeping its id (and its approval, which no
        longer matches if what it does changed)."""
        with self._lock:
            old = self._rules[rid]
            spec = normalise({**rule, "approved": old.spec["approved"],
                              "owner": rule.get("owner") or old.spec["owner"]})
            self._rules[rid] = _Rule(rid, spec)

    def remove(self, rid: str) -> bool:
        with self._lock:
            return self._rules.pop(rid, None) is not None

    def remove_where(self, predicate: Callable[[dict], bool]) -> int:
        with self._lock:
            gone = [rid for rid, r in self._rules.items() if predicate(r.view())]
            for rid in gone:
                del self._rules[rid]
            return len(gone)

    def enable(self, rid: str, on: bool = True) -> None:
        with self._lock:
            rule = self._rules[rid]
            rule.spec["enabled"] = bool(on)
            if on:
                rule.disabled_reason = None
                rule.fire_times.clear()

    def approve(self, rid: str) -> None:
        """A person saw this rule and lets it act as it is now."""
        with self._lock:
            rule = self._rules[rid]
            rule.spec["approved"] = content_hash(rule.spec)

    def list(self) -> list[dict]:
        with self._lock:
            return [r.view() for r in self._rules.values()]

    def get(self, rid: str) -> Optional[dict]:
        with self._lock:
            rule = self._rules.get(rid)
            return rule.view() if rule else None

    def events(self, since: int = 0) -> list[dict]:
        with self._lock:
            return [copy.deepcopy(e) for e in self._events if e["seq"] > since]

    def dump(self, path: str) -> None:
        keep = ("name", "enabled", "when", "limit", "context", "actions", "only_port", "approved")
        with self._lock:
            rules = [{k: r.spec[k] for k in keep} for r in self._rules.values()
                     if r.spec["owner"]["kind"] == "person"]
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"rules": rules}, fh, indent=2, ensure_ascii=False)

    def load(self, path: str, *, approve: bool = False) -> list[str]:
        """Rules from a file. A send rule arrives off and unapproved — someone
        else's file must not type into the device — unless ``approve``."""
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        ids = []
        for rule in data.get("rules", []) if isinstance(data, dict) else data:
            rule = {**rule, "owner": {"kind": "person"}, "approved": None}
            sends = level_of(rule.get("actions") or []) > 0
            if sends and not approve:
                rule["enabled"] = False
            rid = self.add(rule)
            if sends and approve:
                self.approve(rid)
            ids.append(rid)
        return ids

    def close(self) -> None:
        self._stop.set()
        self._unsubscribe()
        if self._timer is not None:
            self._timer.join(timeout=1.0)

    # ── watching ─────────────────────────────────────────────────────────────

    def _on_event(self, event: Event) -> None:
        if event.kind == EventKind.LINE:
            self._on_line(event)
        elif event.kind == EventKind.DATA and event.direction == Direction.RX:
            self._heard()
            self._on_bytes(event.data)
        elif event.kind == EventKind.STATUS:
            self._on_status(event.text)

    def _on_line(self, event: Event) -> None:
        direction = event.direction.value
        origin = (event.meta or {}).get("origin") or {}
        if direction == "tx" and origin.get("via") == "rule":
            return                                  # never a rule set off by a rule
        text = event.text
        span = text[:MATCH_SPAN]
        if direction == "rx":
            self._heard()
        with self._lock:
            candidates = [r for r in self._rules.values() if r.regex is not None
                          and r.spec["when"].get("direction", "rx") == direction]
        for rule in candidates:
            match = rule.regex.search(span)
            if match is None:
                continue
            if direction == "rx" and self._is_own_echo(rule, text):
                continue
            self._matched(rule, text, list(match.groups()))
        if direction == "rx":
            with self._lock:
                self._context.append(text)

    def _heard(self) -> None:
        """The device said something: silences start again."""
        self._last_rx = self._clock()
        with self._lock:
            for rule in self._rules.values():
                rule.silent_fired = False

    def _on_bytes(self, data: bytes) -> None:
        with self._lock:
            rules = [r for r in self._rules.values() if r.kind == "hex"]
            if not rules:
                return
            longest = max(len(r.hex) for r in rules)
            buf = self._hex_tail + data
            start = len(self._hex_tail)
            self._hex_tail = buf[-(longest - 1):] if longest > 1 else b""
        for rule in rules:
            pos = buf.find(rule.hex)
            while pos >= 0:
                if pos + len(rule.hex) > start:       # not already seen in the tail
                    self._matched(rule, rule.spec["when"]["hex"], [])
                pos = buf.find(rule.hex, pos + 1)

    def _on_status(self, state: str) -> None:
        fire = None
        if state == "connected":
            fire = "reconnected" if self._ever_connected and self._dropped else (
                None if self._ever_connected else "connected")
            self._connected, self._ever_connected, self._dropped = True, True, False
            self._last_rx = self._clock()
        elif state in ("error", "reconnecting", "waiting", "disconnected"):
            if self._connected:
                fire = "disconnected"
                self._dropped = True
            self._connected = False
        if fire is None:
            return
        with self._lock:
            rules = [r for r in self._rules.values() if r.kind == "state"]
        for rule in rules:
            if rule.spec["when"]["state"] == fire or (fire == "reconnected" and rule.spec["when"]["state"] == "connected"):
                self._matched(rule, f"device {fire}", [])

    def tick(self) -> None:
        """Check silences; the timer thread calls this every 0.2 s."""
        if not self._connected:
            return
        quiet = self._clock() - self._last_rx
        with self._lock:
            rules = [r for r in self._rules.values()
                     if r.kind == "silence" and not r.silent_fired and quiet >= r.spec["when"]["silence"]]
            for rule in rules:
                rule.silent_fired = True
        for rule in rules:
            self._matched(rule, f"no output for {rule.spec['when']['silence']:g} s", [])

    def _tick_loop(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - a broken tick must not end the timer
                logger.exception("trigger tick failed")

    def _is_own_echo(self, rule: _Rule, text: str) -> bool:
        now = self._clock()
        with self._lock:
            rule.sent = [(t, at) for t, at in rule.sent if now - at <= ECHO_WINDOW]
            return any(text.strip() == t for t, _ in rule.sent)

    # ── firing ───────────────────────────────────────────────────────────────

    def _matched(self, rule: _Rule, line: str, groups: list) -> None:
        now = self._clock()
        with self._lock:
            if rule.id not in self._rules or not rule.spec["enabled"]:
                return
            limit = rule.spec["limit"]
            if limit["after"] > 1:
                rule.match_times.append(now)
                while rule.match_times and now - rule.match_times[0] > limit["window"]:
                    rule.match_times.popleft()
                if len(rule.match_times) < limit["after"]:
                    return
                rule.match_times.clear()
            if rule.last_fire is not None and now - rule.last_fire < limit["cooldown"]:
                return
            while rule.fire_times and now - rule.fire_times[0] > 60:
                rule.fire_times.popleft()
            if len(rule.fire_times) >= limit["max_per_minute"]:
                rule.spec["enabled"] = False
                rule.disabled_reason = f"fired {limit['max_per_minute']} times in a minute"
                storm = True
            else:
                storm = False
                rule.fire_times.append(now)
                rule.last_fire = now
                rule.fired += 1
                if limit["once"]:
                    rule.spec["enabled"] = False
                    rule.disabled_reason = "once"
                context = list(self._context)[-rule.spec["context"]:] if rule.spec["context"] else []
        if storm:
            self.session.publish_notice(f"⚡ {rule.spec['name']}: turned off — {rule.disabled_reason}",
                                        {"trigger": rule.id})
            return
        results = [{"kind": "event", "ok": True}]
        for action in rule.spec["actions"]:
            if action["kind"] in ("mark", "notify"):
                results.append({"kind": action["kind"], "ok": True})
            elif action["kind"] == "send":
                results.append(self._send(rule, action))
        stamp = self.session.tracker.stamp()
        event = {
            "seq": next(self._seq), "rule": rule.id, "name": rule.spec["name"],
            "owner": copy.deepcopy(rule.spec["owner"]),
            "wall": stamp.wall_str(), "elapsed": stamp.elapsed,
            "line": line, "groups": groups, "context": context,
            "mark": any(a["kind"] == "mark" for a in rule.spec["actions"]),
            "notify": any(a["kind"] == "notify" for a in rule.spec["actions"]),
            "actions": results,
        }
        with self._lock:
            self._events.append(event)
        self._write_event(event)
        if event["notify"]:
            self.session.publish_notice(f"⚡ {rule.spec['name']}: {line}", {"trigger": rule.id})
        self.session.bus.publish(Event(kind=EventKind.TRIGGER, direction=Direction.SYS,
                                       stamp=stamp, text=rule.spec["name"], meta=event))

    def _send(self, rule: _Rule, action: dict) -> dict:
        name = rule.spec["name"]
        if rule.spec.get("approved") != content_hash(rule.spec):
            return {"kind": "send", "ok": False, "detail": "not approved as it is now"}
        if not getattr(self.session.source, "writable", True):
            self.session.publish_notice(f"⚡ {name}: not sent — this session cannot be written",
                                        {"trigger": rule.id})
            return {"kind": "send", "ok": False, "detail": "read-only session"}
        if "text" in action:
            data = action["text"].encode(self.session.encoding, errors="replace") + EOLS[action["eol"]]
            sent_text = action["text"].strip()
        else:
            data = bytes.fromhex(action["hex"])
            sent_text = data.decode(self.session.encoding, errors="replace").strip()
        origin = {"via": "rule", "rule": rule.id, "name": name, "owner": copy.deepcopy(rule.spec["owner"])}
        with self._lock:
            rule.sent.append((sent_text, self._clock()))
        try:
            self.session.write(data, origin=origin)
        except Exception as exc:  # noqa: BLE001 - not connected, a write error
            self.session.publish_notice(f"⚡ {name}: not sent — {exc}", {"trigger": rule.id})
            return {"kind": "send", "ok": False, "detail": str(exc)}
        return {"kind": "send", "ok": True, "detail": f"{len(data)} bytes"}

    def _write_event(self, event: dict) -> None:
        if not self.events_path:
            return
        try:
            with open(self.events_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("could not write trigger event to %s", self.events_path, exc_info=True)
