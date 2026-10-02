"""
Device output as text a reader (or an AI agent) can use (SPEC S41).

A console's output is written for a terminal: colours, cursor moves, window
titles, bells. Read as text, those are noise. ``clean_text`` removes terminal
escape sequences and control characters, keeping tab.
"""

from __future__ import annotations

import re

# OSC runs to BEL or ST; CSI to its final byte; any other ESC sequence is ESC,
# intermediates, final; then the C0 controls (but tab) and DEL.
_ESCAPES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b[ -/]*[0-~]?"
    r"|[\x00-\x08\x0a-\x1f\x7f]"
)


def clean_text(text: str) -> str:
    """``text`` without terminal escape sequences or control characters (tab kept)."""
    return _ESCAPES.sub("", text)
