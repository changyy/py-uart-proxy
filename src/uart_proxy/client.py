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
from .proxy.protocol import EOL_MAP, Role, decode_message, encode_message

#: Lines kept, oldest dropped (and counted) beyond this.
DEFAULT_MAX_LINES = 10_000
#: History asked for on connect (the server may have less).
DEFAULT_REPLAY = 500
#: How many lines before a match ``expect`` hands back, for context.
CONTEXT_LINES = 5


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
                 connect_timeout: float = 5.0, name: str = "") -> None:
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
        self.connected = True
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

    def close(self) -> None:
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
                try:
                    self._handle(decode_message(line))
                except ValueError:
                    continue
        finally:
            with self._cond:
                self.connected = False
                self._cond.notify_all()

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
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
                pending = self._asm.pending
                self._partial = (self._line(pending.decode("utf-8", errors="replace"), msg,
                                            n=self._last_n + 1, partial=True) if pending else None)
                self._partial_seq += 1
                self.last_activity = time.time()
                self._cond.notify_all()
        elif kind == "status":
            with self._cond:
                self.state = str(msg.get("state", self.state))
                self._cond.notify_all()

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
