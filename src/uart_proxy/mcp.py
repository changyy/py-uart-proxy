"""
An MCP server for AI tools (SPEC S42).

``uart-proxy mcp`` lets an AI tool — any Model Context Protocol client — read a
session somebody shares, and, with ``--allow-send``, type into it. It is a
*client* of served sessions (S41): whatever holds the port keeps holding it,
where a person can watch what the agent sends and what the device answers.

Transport: MCP over stdio, JSON-RPC 2.0 with one message per line. Only
protocol goes to stdout; anything else goes to stderr. Standard library only.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Optional

from ._version import __version__
from .client import ReadOnlyError, SessionClient, SessionClientError, format_line
from .core.daemon import DaemonNotFound, list_daemons, prune_dead
from .health import assess
from .proxy.protocol import Role

#: Protocol versions this server speaks (the tools subset is the same in each).
KNOWN_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
NEWEST_KNOWN = "2025-06-18"
MAX_LINES = 200
MAX_CHARS = 20_000
MAX_TIMEOUT = 120.0
MAX_DEVICE_WAIT = 600.0
#: How often an attached session's health is looked at, for notifications (S44).
WATCH_SECONDS = 0.5
_NOTE_LEVEL = {"down": "warning", "degraded": "notice", "ok": "info"}

PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602

INSTRUCTIONS = (
    "Serial sessions that a person shares with you, from UARTist or uart-proxy. "
    "Start with list_sessions, then tail to see where the device is. read_new returns "
    "only what you have not seen; wait_for waits for output you have not seen yet. "
    "session_status says whether the device is there; when it is not, tell the person what "
    "its advice says (re-plug the adapter, share the tab again) and call wait_for_device. "
    "The person watching the session sees everything you send. Ask before sending "
    "anything that could change the device irreversibly (bootloader, erase, flash, "
    "factory reset, reboot loops)."
)


class ToolError(Exception):
    """A tool that could not do its job: reported to the agent, not a crash."""


class _Params(Exception):
    """Arguments that do not fit the tool (JSON-RPC -32602)."""


def _session_arg() -> dict:
    return {"type": "string",
            "description": "Session name from list_sessions; optional when only one is shared."}


def _timeout_arg(default: float) -> dict:
    return {"type": "number", "minimum": 0, "maximum": MAX_TIMEOUT,
            "description": f"Seconds to wait (default {default:g}, at most {MAX_TIMEOUT:g})."}


READ_TOOLS = [
    {"name": "list_sessions", "title": "List shared sessions",
     "description": "The serial sessions shared on this computer: name, title, who serves it, "
                    "the port, and whether you may send to it.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "session_status", "title": "Session status",
     "description": "Whether the device is connected, your access (read-only or read & send), "
                    "how many lines have arrived and when the last output came.",
     "inputSchema": {"type": "object", "properties": {"session": _session_arg()}}},
    {"name": "read_new", "title": "Read new output",
     "description": "The device's output since you last called read_new for this session, "
                    "each line with local time and time since the session started.",
     "inputSchema": {"type": "object", "properties": {"session": _session_arg()}}},
    {"name": "tail", "title": "Last lines",
     "description": "The device's last lines (default 50), each with local time and elapsed "
                    "time, plus the line still being written (such as a prompt).",
     "inputSchema": {"type": "object", "properties": {
         "session": _session_arg(),
         "lines": {"type": "integer", "minimum": 1, "maximum": MAX_LINES,
                   "description": "How many lines (default 50)."}}}},
    {"name": "wait_for", "title": "Wait for output",
     "description": "Wait until the device prints a line (or a prompt) matching a regular "
                    "expression, among output you have not seen yet — so a reply that already "
                    "arrived counts. Returns the matching line with the lines before it.",
     "inputSchema": {"type": "object", "required": ["pattern"], "properties": {
         "session": _session_arg(),
         "pattern": {"type": "string", "description": "A regular expression (Python syntax)."},
         "timeout": _timeout_arg(10)}}},
    {"name": "wait_for_device", "title": "Wait for the device",
     "description": "Wait until the session is shared and its device connected — for example after "
                    "asking the person to re-plug the adapter or share the tab again. Returns the "
                    "session's health.",
     "inputSchema": {"type": "object", "properties": {
         "session": _session_arg(),
         "timeout": {"type": "number", "minimum": 0, "maximum": MAX_DEVICE_WAIT,
                     "description": f"Seconds to wait (default 60, at most {MAX_DEVICE_WAIT:g})."}}}},
]

SEND_TOOLS = [
    {"name": "send_text", "title": "Send a line",
     "description": "Type a line into the device, followed by a line ending (default CR), and "
                    "optionally wait for a reply matching wait_for. The person watching sees it.",
     "inputSchema": {"type": "object", "required": ["text"], "properties": {
         "session": _session_arg(),
         "text": {"type": "string", "description": "What to type."},
         "eol": {"type": "string", "enum": ["cr", "lf", "crlf", "none"],
                 "description": "Line ending (default cr)."},
         "wait_for": {"type": "string", "description": "A regular expression to wait for after sending."},
         "timeout": _timeout_arg(10)}}},
    {"name": "send_hex", "title": "Send bytes",
     "description": "Send raw bytes written as hex (\"A5 01 0D\"), for devices that speak a "
                    "binary protocol, and optionally wait for a reply matching wait_for.",
     "inputSchema": {"type": "object", "required": ["hex"], "properties": {
         "session": _session_arg(),
         "hex": {"type": "string", "description": "Bytes as hex, spaces allowed."},
         "wait_for": {"type": "string", "description": "A regular expression to wait for after sending."},
         "timeout": _timeout_arg(10)}}},
]


def _bounded(lines: list[dict], dropped: int = 0) -> tuple[list[dict], int]:
    """At most MAX_LINES lines and MAX_CHARS characters, cutting the oldest."""
    cut = max(0, len(lines) - MAX_LINES)
    lines = lines[cut:]
    total = 0
    keep = len(lines)
    for i in range(len(lines) - 1, -1, -1):
        total += len(lines[i]["text"]) + 40
        if total > MAX_CHARS:
            keep = len(lines) - 1 - i
            break
    cut += len(lines) - keep
    return lines[len(lines) - keep:], dropped + cut


def _render(lines: list[dict], hidden: int, *, head: str = "") -> str:
    out = [head] if head else []
    if hidden:
        out.append(f"({hidden} earlier lines not shown)")
    out += [format_line(l) + ("   [still being written]" if l.get("partial") else "") for l in lines]
    if not lines:
        out.append("(nothing new)")
    return "\n".join(out)


class _Shared:
    """This server's view of one session: its connection, and what it handed back."""

    def __init__(self, client: SessionClient) -> None:
        self.client = client
        self.cursor = 0          # read_new: lines up to here were returned
        self.mark = 0            # wait_for: matched or returned up to here
        self.level: Optional[str] = None   # the verdict last notified (S44)


class McpServer:
    def __init__(self, *, allow_send: bool = False, default_session: Optional[str] = None,
                 write: Optional[Callable[[bytes], None]] = None) -> None:
        self.allow_send = allow_send
        self.default_session = default_session
        self.client_name = "uart-proxy mcp"
        self._write = write or self._stdout
        self._out_lock = threading.Lock()
        self._sessions: dict[str, _Shared] = {}
        self._sessions_lock = threading.Lock()
        self._inflight: set[threading.Thread] = set()
        self._inflight_lock = threading.Lock()
        self.tools = READ_TOOLS + (SEND_TOOLS if allow_send else [])
        self._handlers = {
            "list_sessions": self._list_sessions, "session_status": self._status,
            "read_new": self._read_new, "tail": self._tail, "wait_for": self._wait_for,
            "wait_for_device": self._wait_for_device,
        }
        self._known: set[str] = set()       # sessions attached to before (S44)
        threading.Thread(target=self._watch, daemon=True, name="mcp-health").start()
        if allow_send:
            self._handlers.update(send_text=self._send_text, send_hex=self._send_hex)

    # ── the wire ───────────────────────────────────────────────────────────

    @staticmethod
    def _stdout(data: bytes) -> None:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()

    def _send(self, msg: dict) -> None:
        line = json.dumps(msg, ensure_ascii=False).encode("utf-8") + b"\n"
        with self._out_lock:
            self._write(line)

    def _reply(self, mid, result=None, error=None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": mid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self._send(msg)

    def serve(self, stdin=None) -> int:
        stream = stdin or sys.stdin.buffer
        for raw in stream:
            if raw.strip():
                self.handle_line(raw)
        # Input closed: answer what is still being worked on, then go.
        with self._inflight_lock:
            pending = list(self._inflight)
        for thread in pending:
            thread.join(MAX_TIMEOUT + 5)
        self.close()
        return 0

    def close(self) -> None:
        with self._sessions_lock:
            shared, self._sessions = list(self._sessions.values()), {}
        for s in shared:
            s.client.close()

    def handle_line(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            self._reply(None, error={"code": PARSE_ERROR, "message": f"parse error: {exc}"})
            return
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
            if isinstance(msg, dict) and "method" not in msg and ("result" in msg or "error" in msg):
                return               # a response to something we never sent: ignore
            self._reply(msg.get("id") if isinstance(msg, dict) else None,
                        error={"code": INVALID_REQUEST, "message": "not a JSON-RPC 2.0 request"})
            return
        if "id" not in msg:
            return                   # a notification: initialized, cancelled…
        # Each request on its own thread: a wait_for must not hold up a ping.
        thread = threading.Thread(target=self._run, args=(msg,), daemon=True)
        with self._inflight_lock:
            self._inflight.add(thread)
        thread.start()

    def _run(self, msg: dict) -> None:
        try:
            self._dispatch(msg)
        finally:
            with self._inflight_lock:
                self._inflight.discard(threading.current_thread())

    def _dispatch(self, msg: dict) -> None:
        mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
        try:
            if method == "initialize":
                self._reply(mid, self._initialize(params))
            elif method in ("ping", "logging/setLevel"):
                self._reply(mid, {})
            elif method == "tools/list":
                self._reply(mid, {"tools": self.tools})
            elif method == "tools/call":
                self._reply(mid, self._call(params))
            else:
                self._reply(mid, error={"code": METHOD_NOT_FOUND, "message": f"no method {method!r}"})
        except _Params as exc:
            self._reply(mid, error={"code": INVALID_PARAMS, "message": str(exc)})
        except Exception as exc:  # noqa: BLE001 - one bad call never ends the server
            print(f"uart-proxy mcp: {method}: {exc!r}", file=sys.stderr)
            self._reply(mid, error={"code": -32603, "message": f"internal error: {exc}"})

    def _initialize(self, params: dict) -> dict:
        info = params.get("clientInfo") or {}
        if info.get("name"):
            self.client_name = f"uart-proxy mcp ({str(info['name'])[:40]})"
        asked = params.get("protocolVersion")
        return {
            "protocolVersion": asked if asked in KNOWN_VERSIONS else NEWEST_KNOWN,
            "capabilities": {"tools": {"listChanged": False}, "logging": {}},
            "serverInfo": {"name": "uart-proxy", "title": "uart-proxy serial sessions",
                           "version": __version__},
            "instructions": INSTRUCTIONS,
        }

    def _call(self, params: dict) -> dict:
        name = params.get("name")
        handler = self._handlers.get(name)
        if handler is None:
            raise _Params(f"unknown tool {name!r}"
                          + (" (sending needs 'uart-proxy mcp --allow-send')"
                             if name in ("send_text", "send_hex") else ""))
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise _Params("arguments must be an object")
        try:
            text, data = handler(args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        verdict = data.pop("_verdict", None)
        if verdict is not None and verdict["level"] != "ok":
            # S44: a result from a session that is not well says so, first.
            data["health"] = {"level": verdict["level"], "summary": verdict["summary"],
                              "advice": verdict["advice"]}
            text = f"⚠ {verdict['summary']} {verdict['advice']}".rstrip() + "\n" + text
        return {"content": [{"type": "text", "text": text}], "structuredContent": data, "isError": False}

    # ── sessions ───────────────────────────────────────────────────────────

    def _shared(self, args: dict) -> _Shared:
        name = args.get("session") or self.default_session
        if name is not None and not isinstance(name, str):
            raise _Params("session must be a string")
        prune_dead()
        try:
            client = SessionClient.from_registry(name, want_full=self.allow_send,
                                                 client_name=self.client_name)
        except DaemonNotFound as exc:
            gone = (name or (next(iter(self._known)) if len(self._known) == 1 else None))
            if gone in self._known:
                verdict = assess(shared=False, device=None, link_error="it is no longer listed")
                raise ToolError(f"{gone}: {verdict['summary']} {verdict['advice']}") from exc
            raise ToolError(str(exc)) from exc
        key = client.name
        with self._sessions_lock:
            shared = self._sessions.get(key)
            if shared is not None and shared.client.connected:
                return shared
            try:
                client.connect()
            except SessionClientError as exc:
                raise ToolError(f"cannot reach session {key!r}: {exc}") from exc
            shared = _Shared(client)
            self._sessions[key] = shared
            self._known.add(key)
            client.on_device = lambda _d, s=shared, k=key: self._check(k, s)
            shared.level = client.health()["level"]
            return shared

    @staticmethod
    def _timeout(args: dict, default: float = 10.0) -> float:
        try:
            value = float(args.get("timeout", default))
        except (TypeError, ValueError):
            raise _Params("timeout must be a number")
        return min(max(value, 0.0), MAX_TIMEOUT)

    def _list_sessions(self, args: dict):
        prune_dead()
        sessions = []
        for info in list_daemons():
            roles = set((info.codes or {info.auth: "full"}).values())
            sessions.append({"name": info.name, "title": info.title, "owner": info.owner,
                             "port": info.port, "baud": info.baud,
                             "can_send": self.allow_send and "full" in roles})
        if not sessions:
            return ("No session is shared. In UARTist, turn on \"Share with AI\" for a tab; "
                    "with uart-proxy, run 'uart-proxy connect --serve' or 'uart-proxy start'.",
                    {"sessions": []})
        text = "\n".join(
            f"{s['name']}: {s['title'] or s['port']} (port {s['port']} @ {s['baud']}, "
            f"{s['owner']}, {'read & send' if s['can_send'] else 'read-only'})" for s in sessions)
        return text, {"sessions": sessions}

    def _status(self, args: dict):
        s = self._shared(args)
        c = s.client
        access = "read & send" if self.allow_send and c.role == Role.FULL else "read-only"
        v = c.health()
        dev = v["device"]
        device = {k: dev.get(k) for k in ("state", "since", "error", "reconnects", "last_output")}
        device["silent_for"] = round(dev["silent_for"], 1) if dev.get("silent_for") is not None else None
        data = {"session": c.name, "source": c.source, "health": v["level"], "summary": v["summary"],
                "advice": v["advice"], "access": access, "lines": c.cursor, "connected": c.connected,
                "share": v["share"], "device": device}
        text = (f"{c.name}: {v['level'].upper()} — {v['summary']}"
                + (f" {v['advice']}" if v["advice"] else "")
                + f"\nsource {c.source}; {access}; {c.cursor} lines; device {device['state']}"
                + (f" since {device['since']}" if device.get("since") else "")
                + (f", {device['reconnects']} reconnects" if device.get("reconnects") else ""))
        return text, data

    def _read_new(self, args: dict):
        s = self._shared(args)
        lines, cursor, dropped = s.client.read(s.cursor)
        s.cursor = cursor
        s.mark = max(s.mark, cursor)
        partial = s.client.partial
        shown, hidden = _bounded(lines, dropped)
        text = _render(shown + ([partial] if partial else []), hidden)
        return text, {"lines": shown, "partial": partial, "not_shown": hidden, "_verdict": s.client.health()}

    def _tail(self, args: dict):
        s = self._shared(args)
        try:
            n = int(args.get("lines", 50))
        except (TypeError, ValueError):
            raise _Params("lines must be an integer")
        lines = s.client.tail(max(1, min(n, MAX_LINES)))
        partial = s.client.partial
        shown, hidden = _bounded(lines)
        return _render(shown + ([partial] if partial else []), hidden), \
            {"lines": shown, "partial": partial, "_verdict": s.client.health()}

    def _wait(self, s: _Shared, pattern, timeout: float, since: int):
        if not isinstance(pattern, str) or not pattern:
            raise _Params("pattern must be a non-empty string")
        try:
            hit = s.client.expect(pattern, timeout, since=since)
        except Exception as exc:  # re.error: a bad pattern is the agent's to fix
            raise ToolError(f"bad pattern {pattern!r}: {exc}") from exc
        if hit is None:
            seen = s.client.tail(5)
            v = s.client.health()
            why = (f"{v['summary']} {v['advice']}".strip() if v["level"] != "ok" or v["advice"]
                   else "The device is connected.")
            raise ToolError(f"no output matching {pattern!r} within {timeout:g}s. {why}\n"
                            f"Last lines:\n" + _render(seen, 0))
        line = hit["line"]
        s.mark = max(s.mark, line["n"] if not line.get("partial") else s.client.cursor)
        text = _render(hit["before"] + [line], 0, head=f"matched {pattern!r}:")
        return text, {"match": line, "before": hit["before"], "_verdict": s.client.health()}

    def _wait_for(self, args: dict):
        s = self._shared(args)
        return self._wait(s, args.get("pattern"), self._timeout(args), max(s.mark, s.cursor))

    def _wait_for_device(self, args: dict):
        try:
            timeout = min(max(float(args.get("timeout", 60)), 0.0), MAX_DEVICE_WAIT)
        except (TypeError, ValueError):
            raise _Params("timeout must be a number")
        deadline = time.monotonic() + timeout
        last: Optional[str] = None
        while True:
            try:
                s = self._shared(args)          # joins again if it was shared anew
                v = s.client.health()
                if s.client.connected and v["device"].get("state") == "connected":
                    text, data = self._status(args)
                    return f"The device is back.\n{text}", data
                last = f"{v['summary']} {v['advice']}".strip()
            except ToolError as exc:
                last = str(exc)
            if time.monotonic() >= deadline:
                raise ToolError(f"still not back after {timeout:g}s. {last}")
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    # ── notifications (S44) ─────────────────────────────────────────────────

    def _watch(self) -> None:
        """Re-look at attached sessions: a verdict also changes with time (settling)."""
        while True:
            time.sleep(WATCH_SECONDS)
            with self._sessions_lock:
                shared = list(self._sessions.items())
            for key, s in shared:
                self._check(key, s)

    def _check(self, key: str, s: "_Shared") -> None:
        v = s.client.health()
        if v["level"] == s.level:
            return
        s.level = v["level"]
        self._send({"jsonrpc": "2.0", "method": "notifications/message",
                    "params": {"level": _NOTE_LEVEL[v["level"]], "logger": "uart-proxy",
                               "data": {"session": key, "health": v["level"],
                                        "summary": v["summary"], "advice": v["advice"]}}})

    def _send_and_wait(self, args: dict, send: Callable[[SessionClient], None], what: str):
        s = self._shared(args)
        mark = s.client.cursor
        try:
            send(s.client)
        except ReadOnlyError as exc:
            raise ToolError(f"{exc}: the person sharing it chose read-only") from exc
        except (SessionClientError, ValueError, OSError) as exc:
            raise ToolError(f"could not send: {exc}") from exc
        pattern = args.get("wait_for")
        if not pattern:
            return f"sent {what}", {"sent": what}
        text, data = self._wait(s, pattern, self._timeout(args), mark)
        return f"sent {what}\n{text}", {"sent": what, **data}

    def _send_text(self, args: dict):
        text = args.get("text")
        if not isinstance(text, str):
            raise _Params("text must be a string")
        eol = args.get("eol", "cr")
        if eol not in ("cr", "lf", "crlf", "none"):
            raise _Params("eol must be cr, lf, crlf or none")
        return self._send_and_wait(args, lambda c: c.send_text(text, eol=eol), repr(text))

    def _send_hex(self, args: dict):
        value = args.get("hex")
        if not isinstance(value, str):
            raise _Params("hex must be a string")
        return self._send_and_wait(args, lambda c: c.send_hex(value), value)


def cmd_mcp(args) -> int:
    server = McpServer(allow_send=args.allow_send, default_session=args.session)
    print(f"uart-proxy mcp {__version__}: serving MCP on stdio "
          f"({'read & send' if args.allow_send else 'read-only'})", file=sys.stderr)
    return server.serve()


def add_parser(sub) -> None:
    p = sub.add_parser("mcp", help="An MCP server (stdio) for AI tools to read shared sessions.")
    p.add_argument("--allow-send", action="store_true",
                   help="Also offer send_text / send_hex — for sessions shared with full access.")
    p.add_argument("--session", default=None, metavar="NAME",
                   help="The session to use when a tool call names none.")
    p.set_defaults(func=cmd_mcp)
