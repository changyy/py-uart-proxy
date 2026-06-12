"""
Event model shared across the whole pipeline.

A single :class:`Event` type flows through the :class:`~uart_proxy.core.bus.EventBus`
to every consumer (recorder, plugins, proxy server, UI). Producers are the
session's read loop (RX) and ``write`` calls (TX), plus plugins/system
(NOTICE / STATUS).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

from .timestamp import Stamp


class EventKind(str, enum.Enum):
    """What a given event represents."""

    DATA = "data"      # a raw byte chunk as it arrived/left (live stream + raw log)
    LINE = "line"      # a fully assembled line (timestamped logs + plugins)
    NOTICE = "notice"  # a plugin or system message worth surfacing
    STATUS = "status"  # a connection/session state change


class Direction(str, enum.Enum):
    """Which way the bytes travelled."""

    RX = "rx"   # received from the device / remote
    TX = "tx"   # sent to the device / remote
    SYS = "sys"  # produced by the app itself (notices, status)


@dataclass
class Event:
    """One thing that happened, stamped on both time axes."""

    kind: EventKind
    direction: Direction
    stamp: Stamp
    seq: int = 0
    data: bytes = b""           # raw bytes (DATA, and the raw bytes of a LINE)
    text: str = ""              # decoded text
    meta: dict[str, Any] = field(default_factory=dict)
