"""
The socket proxy wire protocol.

One JSON object per line (``\\n`` terminated, UTF-8). This is deliberately
simple and language-agnostic so a future mobile client (Flutter) can speak it
trivially.

Handshake
---------
The client's first line MUST be an auth message::

    {"type": "auth", "code": "123456"}

The server replies with either::

    {"type": "auth_ok", "role": "full", "source": "/dev/tty.usbserial @ 115200 8N1"}

or::

    {"type": "auth_fail", "reason": "invalid code"}

Roles
-----
* ``full``     — may read the stream and send ``tx`` commands.
* ``readonly`` — may only read the stream (the "limited" mode a mobile client
  would typically use). ``tx`` from a readonly client is rejected.

Server → client (after auth)
----------------------------
* ``{"type":"rx","seq":N,"wall":"...","elapsed":F,"hex":"...","text":"..."}``
* ``{"type":"notice","text":"...","meta":{...}}``
* ``{"type":"status","state":"...","meta":{...}}``

Client → server (after auth)
----------------------------
* ``{"type":"tx","hex":"..."}``      send raw bytes (hex)            [full only]
* ``{"type":"tx","text":"...","eol":"crlf"}`` send text + line ending [full only]
* ``{"type":"ping"}``                → server replies ``{"type":"pong"}``
"""

from __future__ import annotations

import enum
import json
from typing import Any


class Role(str, enum.Enum):
    FULL = "full"
    READONLY = "readonly"


EOL_MAP: dict[str, bytes] = {
    "crlf": b"\r\n",
    "lf": b"\n",
    "cr": b"\r",
    "none": b"",
}


def encode_message(obj: dict[str, Any]) -> bytes:
    """Serialise a message to a single newline-terminated UTF-8 line."""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def decode_message(line: bytes) -> dict[str, Any]:
    """Parse one line into a message dict. Raises ValueError on bad input."""
    try:
        obj = json.loads(line.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError("message must be a JSON object")
    return obj


def parse_auth_spec(spec: str) -> tuple[str, Role]:
    """
    Parse a CLI auth spec ``"CODE"`` or ``"CODE:role"`` into ``(code, role)``.

    Defaults to the ``full`` role when no role is given.
    """
    if ":" in spec:
        code, _, role_str = spec.partition(":")
        role_str = role_str.strip().lower()
        try:
            role = Role(role_str)
        except ValueError as exc:
            raise ValueError(
                f"unknown role {role_str!r} (use 'full' or 'readonly')"
            ) from exc
        return code.strip(), role
    return spec.strip(), Role.FULL
