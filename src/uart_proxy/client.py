"""
A programmatic client for a served session (SPEC S41).

``attach`` is a person's view of a running session. This is the one a script,
a test or an AI agent uses: what the device said lately, waiting until it says
something, and sending a line. It speaks the proxy protocol (PROTOCOL.md) like
any other client — so it never owns the port, and whoever does (a ``connect
--serve``, a background session, an application serving one of its tabs) keeps
seeing everything it sends and everything the device answers.

    client = SessionClient.from_registry("usbserial-110", client_name="my test")
    client.connect()
    mark = client.cursor
    client.send_text("uname -a")
    hit = client.expect(r"Linux", timeout=5, since=mark)
"""

from __future__ import annotations

import queue
import re
import socket
import threading
import time
from collections import deque
from typing import Optional

from .core.daemon import connect_host, find_daemon
from .core.line_assembler import LineAssembler
from .core.text import clean_text
from .core.timestamp import format_elapsed
from .health import assess
from .proxy.protocol import EOL_MAP, Role, decode_message, encode_message

#: Lines kept, oldest dropped (and counted) beyond this.
DEFAULT_MAX_LINES = 10_000
#: History asked for on connect (the server may have less).
DEFAULT_REPLAY = 500
#: How many lines before a match ``expect`` hands back, for context.
CONTEXT_LINES = 5
#: Seconds between pings; three without a word back is a lost link (S43).
DEFAULT_HEARTBEAT = 5.0
#: A server from before S43 sends no device state: it was taken as connected.
_LEGACY_DEVICE = {"state": "connected", "since": None, "since_epoch": None, "error": None,
                  "reconnects": 0, "last_output": None, "last_output_epoch": None}


class SessionClientError(Exception):
    """The session cannot be reached, or refused us."""


class ReadOnlyError(SessionClientError):
    """This connection may only read (a ``readonly`` code, SPEC S6)."""


def _hex_bytes(text: str) -> bytes:
    digits = "".join(text.split())
    try:
        return bytes.fromhex(digits)
    except ValueError as exc:
        raise ValueError(f"not hex bytes: {text!r} (write them like 41 42 0d)") from exc


class SessionClient:
    def __init__(self, host: str, port: int, code: str, *, client_name: str = "",
                 replay: int = DEFAULT_REPLAY, max_lines: int = DEFAULT_MAX_LINES,
                 connect_timeout: float = 5.0, name: str = "",
                 heartbeat: float = DEFAULT_HEARTBEAT) -> None:
        self.host, self.port, self.code = host, port, code
        self.client_name = client_name
        #: The registry name, when it came from there.
        self.name = name
        self._replay = replay
        self._connect_timeout = connect_timeout
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[threading.Thread] = None
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._lines: "deque[dict]" = deque(maxlen=max(1, max_lines))
        self._last_n = 0
        self._asm = LineAssembler()
        self._partial: Optional[dict] = None
        self._partial_seq = 0
        self.role: Optional[Role] = None
        self.source = ""
        self.state = "connected"
        self.connected = False
        self.error: Optional[str] = None
        self.last_activity: Optional[float] = None
        # S43: the device as the session last described it, and our own link.
        self.device: dict = dict(_LEGACY_DEVICE)
        #: Called with the device dict whenever its state changes.
        self.on_device = None
        #: Called with each trigger event (S47).
        self.on_trigger = None
        self.link_error: Optional[str] = None
        self._heartbeat = heartbeat
        self._last_heard = time.monotonic()
        self._closing = False
        # S47: replies to our requests, trigger events, proposals in the owner's hands.
        self._replies: "queue.Queue[dict]" = queue.Queue()
        self._request_lock = threading.Lock()
        self._events: "deque[dict]" = deque(maxlen=500)
        self._awaiting: set[str] = set()
        self._decided: dict[str, dict] = {}

    # ── finding and joining a session ──────────────────────────────────────

    @classmethod
    def from_registry(cls, name: Optional[str] = None, *, want_full: bool = False,
                      **kw) -> "SessionClient":
        """The session registered as ``name`` (or the only one running, S17).

        Takes its read-only code unless ``want_full``; a session that has only
        one kind of code gets that one.
        """
        info = find_daemon(name)
        codes = info.codes or {info.auth: "full"}
        want = "full" if want_full else "readonly"
        code = next((c for c, r in codes.items() if r == want), None) or next(iter(codes))
        return cls(connect_host(info.listen_host), info.listen_port, code, name=info.name, **kw)

    def connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self.port), timeout=self._connect_timeout)
        except OSError as exc:
            raise SessionClientError(f"cannot reach {self.host}:{self.port}: {exc}") from exc
        self._sock = sock
        hello = {"type": "auth", "code": self.code, "replay": self._replay}
        if self.client_name:
            hello["client"] = self.client_name
        self._send(hello)
        buf = bytearray()
        reply = self._read_line(buf)
        if reply is None:
            self.close()
            raise SessionClientError("no answer from the session")
        msg = decode_message(reply)
        if msg.get("type") != "auth_ok":
            self.close()
            raise SessionClientError(f"refused: {msg.get('reason', 'unknown')}")
        self.role = Role(msg.get("role", "readonly"))
        self.source = str(msg.get("source", ""))
        if isinstance(msg.get("device"), dict):
            self.device = {**_LEGACY_DEVICE, **msg["device"]}
            self.state = self.device["state"]
        self.connected = True
        self.link_error = None
        self._last_heard = time.monotonic()
        # The replay block (S18) comes before any live traffic, ending in
        # replay_end — read it here so it is in hand when connect() returns.
        if self._replay > 0:
            deadline = time.monotonic() + self._connect_timeout
            while time.monotonic() < deadline:
                line = self._read_line(buf)
                if line is None:
                    break
                msg = decode_message(line)
                if msg.get("type") == "replay_end":
                    break
                self._handle(msg)
        sock.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, args=(buf,), daemon=True,
                                        name=f"session-client-{self.port}")
        self._reader.start()
        if self._heartbeat > 0:
            threading.Thread(target=self._beat, daemon=True, name=f"session-heartbeat-{self.port}").start()

    def close(self) -> None:
        self._closing = True
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        with self._cond:
            self.connected = False
            self._cond.notify_all()

    def __enter__(self) -> "SessionClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── what the device said ───────────────────────────────────────────────

    @property
    def cursor(self) -> int:
        """The newest line's ``n``: pass it to ``read`` / ``expect`` later."""
        with self._cond:
            return self._last_n

    def read(self, cursor: int = 0, limit: Optional[int] = None) -> tuple[list[dict], int, int]:
        """Lines after ``cursor``, the new cursor, and how many were lost between."""
        with self._cond:
            lines = [dict(l) for l in self._lines if l["n"] > cursor]
            first = self._lines[0]["n"] if self._lines else self._last_n + 1
            dropped = max(0, first - cursor - 1) if self._last_n > cursor else 0
        if limit is not None and len(lines) > limit:
            dropped += len(lines) - limit
            lines = lines[-limit:]
        return lines, (lines[-1]["n"] if lines else max(cursor, self.cursor)), dropped

    def tail(self, n: int = 50) -> list[dict]:
        with self._cond:
            return [dict(l) for l in list(self._lines)[-n:]] if n > 0 else []

    @property
    def partial(self) -> Optional[dict]:
        """The line still being written (a prompt), if any."""
        with self._cond:
            return dict(self._partial) if self._partial else None

    def expect(self, pattern: str, timeout: float = 10.0, *,
               since: Optional[int] = None) -> Optional[dict]:
        """Wait for a line after ``since`` (default: now) — or the partial line —
        matching ``pattern``; ``{"line", "before"}``, or ``None`` on timeout."""
        regex = re.compile(pattern)
        deadline = time.monotonic() + max(0.0, timeout)
        with self._cond:
            after = self._last_n if since is None else since
            partial_seen = self._partial_seq if since is None else -1
            while True:
                for line in self._lines:
                    if line["n"] > after and regex.search(line["text"]):
                        return {"line": dict(line), "before": self._before(line["n"])}
                if self._partial is not None and self._partial_seq != partial_seen \
                        and regex.search(self._partial["text"]):
                    return {"line": dict(self._partial), "before": self._before(self._last_n + 1)}
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self.connected:
                    return None
                self._cond.wait(remaining)

    def _before(self, n: int) -> list[dict]:
        return [dict(l) for l in self._lines if n - CONTEXT_LINES <= l["n"] < n]

    # ── sending ────────────────────────────────────────────────────────────

    def send_text(self, text: str, eol: str = "cr") -> None:
        if eol not in EOL_MAP:
            raise ValueError(f"eol must be one of {', '.join(EOL_MAP)}")
        self._require_full()
        self._send({"type": "tx", "text": text, "eol": eol})

    def send_hex(self, text: str) -> None:
        data = _hex_bytes(text)
        self._require_full()
        self._send({"type": "tx", "hex": data.hex()})

    def _require_full(self) -> None:
        if self.role != Role.FULL:
            raise ReadOnlyError("this session is shared read-only: it cannot be written to")
        if not self.connected:
            raise SessionClientError("not connected to the session")

    # ── the wire ───────────────────────────────────────────────────────────

    def _send(self, msg: dict) -> None:
        sock = self._sock
        if sock is None:
            raise SessionClientError("not connected to the session")
        with self._send_lock:
            sock.sendall(encode_message(msg))

    def _read_line(self, buf: bytearray) -> Optional[bytes]:
        sock = self._sock
        while sock is not None:
            idx = buf.find(b"\n")
            if idx >= 0:
                line = bytes(buf[:idx])
                del buf[:idx + 1]
                return line
            try:
                chunk = sock.recv(65536)
            except (OSError, socket.timeout):
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return None

    def _read_loop(self, buf: bytearray) -> None:
        try:
            while True:
                line = self._read_line(buf)
                if line is None:
                    break
                self._last_heard = time.monotonic()
                try:
                    self._handle(decode_message(line))
                except ValueError:
                    continue
        finally:
            with self._cond:
                if not self._closing and self.link_error is None:
                    self.link_error = "the session closed the connection"
                self.connected = False
                self._cond.notify_all()

    def _beat(self) -> None:
        """Ping, and count the link lost when nothing has come back for three beats."""
        while self.connected and not self._closing:
            time.sleep(self._heartbeat)
            if not self.connected or self._closing:
                return
            if time.monotonic() - self._last_heard > 3 * self._heartbeat:
                self.link_error = "no answer from the session"
                sock, self._sock = self._sock, None
                if sock is not None:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    sock.close()
                with self._cond:
                    self.connected = False
                    self._cond.notify_all()
                return
            try:
                self._send({"type": "ping"})
            except (OSError, SessionClientError):
                pass

    # ── health (S43) ───────────────────────────────────────────────────────

    def health(self) -> dict:
        """The verdict on this session: link and device, with advice."""
        now = time.time()
        with self._cond:
            device = dict(self.device)
            shared = self.connected
        since = device.get("since_epoch")
        last = device.get("last_output_epoch")
        device["since_age"] = (now - since) if since else None
        device["silent_for"] = (now - last) if last else None
        verdict = assess(shared=shared, device=device if shared else None, link_error=self.link_error)
        return {**verdict, "device": device, "share": {"connected": shared, "error": self.link_error}}

    # ── triggers (S47) ─────────────────────────────────────────────────────

    def _request(self, msg: dict, ok_type: str, timeout: float = 5.0) -> dict:
        """Send ``msg`` and wait for its answer (``ok_type``, or a refusal)."""
        with self._request_lock:
            while not self._replies.empty():
                self._replies.get_nowait()
            self._send(msg)
            deadline = time.monotonic() + timeout
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise SessionClientError(f"no answer to {msg.get('type')}")
                try:
                    reply = self._replies.get(timeout=left)
                except queue.Empty:
                    continue
                if reply.get("type") == "watch_fail":
                    raise SessionClientError(str(reply.get("reason") or "refused"))
                if reply.get("type") == ok_type:
                    return reply

    def watch_add(self, when: dict, *, name: str = "", limit: Optional[dict] = None,
                  context: int = 3) -> str:
        """Ask the session to tell us when ``when`` happens; returns the watch's id."""
        msg: dict = {"type": "watch_add", "when": when, "name": name or "watch", "context": context}
        if limit:
            msg["limit"] = limit
        return str(self._request(msg, "watch_ok")["id"])

    def watch_remove(self, wid: str) -> None:
        self._request({"type": "watch_remove", "id": wid}, "watch_ok")

    def watch_list(self) -> list[dict]:
        return list(self._request({"type": "watch_list"}, "watch_list").get("watches", []))

    def propose_rule(self, rule: dict, timeout: float = 150.0) -> dict:
        """Propose a rule that acts; the session's owner decides. Returns the
        ``proposal`` answer: ``status`` refused, declined or accepted (``rule``)."""
        first = self._request({"type": "rule_propose", "rule": rule}, "proposal")
        if first.get("status") != "pending":
            return first
        pid = str(first.get("id"))
        deadline = time.monotonic() + timeout
        with self._cond:
            while pid not in self._decided:
                left = deadline - time.monotonic()
                if left <= 0 or not self.connected:
                    self._awaiting.discard(pid)
                    return {"type": "proposal", "id": pid, "status": "unanswered"}
                self._cond.wait(min(left, 0.5))
            self._awaiting.discard(pid)
            return self._decided.pop(pid)

    def events(self, since: int = 0) -> list[dict]:
        """The trigger events heard, after ``since`` (an event's ``seq``)."""
        with self._cond:
            return [dict(e) for e in self._events if int(e.get("seq", 0)) > since]

    def wait_event(self, timeout: float = 10.0, *, watch: Optional[str] = None,
                   since: Optional[int] = None) -> Optional[dict]:
        """The next trigger event (of ``watch``, if given) after ``since`` —
        by default, from the moment of the call; ``None`` on timeout."""
        deadline = time.monotonic() + timeout
        with self._cond:
            if since is None:
                since = max((int(e.get("seq", 0)) for e in self._events), default=0)
            while True:
                for event in self._events:
                    if int(event.get("seq", 0)) > since and (watch is None or event.get("rule") == watch):
                        return dict(event)
                left = deadline - time.monotonic()
                if left <= 0 or not self.connected:
                    return None
                self._cond.wait(min(left, 0.5))

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind in ("watch_ok", "watch_fail", "watch_list"):
            self._replies.put(msg)
            return
        if kind == "proposal":
            pid = str(msg.get("id"))
            with self._cond:
                if msg.get("status") == "pending":
                    self._awaiting.add(pid)
                elif pid in self._awaiting:
                    self._decided[pid] = msg
                    self._cond.notify_all()
                    return
            self._replies.put(msg)
            return
        if kind == "trigger":
            event = {k: v for k, v in msg.items() if k != "type"}
            with self._cond:
                self._events.append(event)
                self._cond.notify_all()
            callback = self.on_trigger
            if callback is not None:
                try:
                    callback(dict(event))
                except Exception:  # noqa: BLE001 - a listener's bug is not the link's
                    pass
            return
        if kind == "replay":
            self._add(str(msg.get("text", "")), msg, replayed=True)
        elif kind == "rx":
            try:
                data = bytes.fromhex(str(msg.get("hex", "")))
            except ValueError:
                return
            with self._cond:
                lines = self._asm.feed(data)
            for raw in lines:
                self._add(raw.decode("utf-8", errors="replace"), msg)
            with self._cond:
                self.device["last_output"] = str(msg.get("wall", "")) or self.device.get("last_output")
                self.device["last_output_epoch"] = time.time()
                pending = self._asm.pending
                self._partial = (self._line(pending.decode("utf-8", errors="replace"), msg,
                                            n=self._last_n + 1, partial=True) if pending else None)
                self._partial_seq += 1
                self.last_activity = time.time()
                self._cond.notify_all()
        elif kind == "status":
            with self._cond:
                self.state = str(msg.get("state", self.state))
                self.device["state"] = self.state
                for key in ("since", "since_epoch", "reconnects"):
                    if key in msg:
                        self.device[key] = msg[key]
                if "error" in msg:
                    self.device["error"] = msg["error"]
                device = dict(self.device)
                self._cond.notify_all()
            callback = self.on_device
            if callback is not None:
                try:
                    callback(device)
                except Exception:  # noqa: BLE001 - a listener's bug is not the link's
                    pass

    @staticmethod
    def _line(text: str, msg: dict, *, n: int, replayed: bool = False,
              partial: bool = False) -> dict:
        try:
            elapsed = format_elapsed(float(msg.get("elapsed", 0.0)))
        except (TypeError, ValueError):
            elapsed = format_elapsed(0.0)
        return {"n": n, "wall": str(msg.get("wall", "")), "elapsed": elapsed,
                "text": clean_text(text), "replayed": replayed, "partial": partial}

    def _add(self, text: str, msg: dict, *, replayed: bool = False) -> None:
        with self._cond:
            self._last_n += 1
            self._lines.append(self._line(text, msg, n=self._last_n, replayed=replayed))
            self.last_activity = time.time()
            self._cond.notify_all()


def format_line(line: dict) -> str:
    """A line as the timestamped log shows it: ``wall | elapsed  text``."""
    return f"{line['wall']} | {line['elapsed']}  {line['text']}"
