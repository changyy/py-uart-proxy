"""
Turning keystrokes into the bytes a serial console expects.

Line mode collects a line and sends it on Enter, which is comfortable for typing
commands but cannot express anything a shell needs *now* — ``^C`` to interrupt,
``^D`` for EOF, Tab to complete, ↑ for history. Character mode sends each key as
it is pressed, which is what every serial terminal does, and needs this table.

Two conventions worth knowing, because both have bitten people:

* **Backspace sends DEL (0x7F), not BS (0x08).** Unix consoles and readline
  expect DEL; sending BS shows up as ``^H`` on many devices. PuTTY and minicom
  default to DEL for the same reason. Configurable per session all the same.
* **Arrow keys are escape sequences** (``ESC [ A``…), so they must be emitted
  whole. That is also why ``ESC`` on its own cannot be treated as a "signal" that
  jumps a queue: it is the first byte of something longer.
"""

from __future__ import annotations

from typing import Optional

#: Textual key name → the bytes a terminal would send. Arrow and navigation keys
#: use the ANSI/DEC sequences a device's line editor recognises.
_NAMED: dict[str, bytes] = {
    "space": b" ",
    "tab": b"\t",
    "escape": b"\x1b",
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "home": b"\x1b[H",
    "end": b"\x1b[F",
    "delete": b"\x1b[3~",
    "insert": b"\x1b[2~",
    "pageup": b"\x1b[5~",
    "pagedown": b"\x1b[6~",
    "f1": b"\x1bOP",
    "f2": b"\x1bOQ",
    "f3": b"\x1bOR",
    "f4": b"\x1bOS",
}

#: Control keys whose Textual name isn't simply ``ctrl+<letter>``.
_CTRL_SYMBOLS: dict[str, int] = {
    "left_square_bracket": 0x1B,   # ^[  = ESC
    "backslash": 0x1C,             # ^\  = QUIT
    "right_square_bracket": 0x1D,  # ^]
    "circumflex_accent": 0x1E,     # ^^
    "underscore": 0x1F,            # ^_
    "space": 0x00,                 # ^@  = NUL
    "at": 0x00,
}

#: Punctuation a user may type in ``--prefix`` instead of Textual's name for it.
_PREFIX_ALIASES: dict[str, str] = {
    "]": "right_square_bracket",
    "[": "left_square_bracket",
    "\\": "backslash",
    "^": "circumflex_accent",
    "_": "underscore",
    "@": "at",
}

BACKSPACE_DEL = b"\x7f"
BACKSPACE_BS = b"\x08"

#: The one key the UI keeps for itself, so every other key can go to the device.
#:
#: ``Ctrl+]`` is telnet's escape, chosen there for exactly this problem. The
#: obvious alternatives are worse for a *serial console*, because what is usually
#: on the far end is a shell: ``screen``'s ``Ctrl+A`` is readline's line-start and
#: tmux's ``Ctrl+B`` is back-one-character, so both get pressed by accident all
#: day. ``Ctrl+]`` means nothing to readline and nothing to flow control (unlike
#: ``Ctrl+Q``, which is XON). Whatever it is, ``<prefix> <prefix>`` sends the
#: literal byte, so the choice is never a dead end.
DEFAULT_PREFIX = "ctrl+right_square_bracket"


def key_to_bytes(
    key: str,
    character: Optional[str],
    *,
    eol: bytes = b"\r",
    backspace: bytes = BACKSPACE_DEL,
    encoding: str = "utf-8",
) -> Optional[bytes]:
    """The bytes to put on the wire for one key press, or None if there are none.

    ``key`` and ``character`` are Textual's :class:`~textual.events.Key` fields.
    ``None`` means "nothing to send" — a modifier, a function key we have no
    sequence for — and the caller should simply ignore it rather than sending a
    placeholder the device would have to interpret.
    """
    if key == "enter":
        return eol
    if key == "backspace":
        return backspace
    if key in _NAMED:
        return _NAMED[key]

    if key.startswith("ctrl+"):
        rest = key[len("ctrl+"):]
        if len(rest) == 1 and rest.isalpha():
            return bytes((ord(rest.lower()) & 0x1F,))
        if rest in _CTRL_SYMBOLS:
            return bytes((_CTRL_SYMBOLS[rest],))
        return None

    # A printable character (Textual gives us the character for these; control
    # keys arrive with character=None, so this can't misfire on them).
    if character and character.isprintable():
        return character.encode(encoding, errors="replace")
    # Textual reports the control character for a few keys (tab, escape…), which
    # the tables above have already handled; anything left is not for the wire.
    return None


def normalise_prefix(spec: str) -> str:
    """Turn a user's ``--prefix`` into the key name Textual will report.

    Accepts ``ctrl+]``, ``ctrl-]``, ``^]``, or Textual's own
    ``ctrl+right_square_bracket``. Raises ValueError on anything that could not
    be a single control key, because silently picking a different prefix from the
    one asked for would leave the user pressing a key that does nothing.
    """
    text = spec.strip().lower().replace("-", "+")
    if text.startswith("^") and len(text) == 2:
        text = f"ctrl+{text[1]}"
    if not text.startswith("ctrl+"):
        raise ValueError(f"prefix must be a Ctrl key, e.g. 'ctrl+]' (got {spec!r})")
    rest = text[len("ctrl+"):]
    rest = _PREFIX_ALIASES.get(rest, rest)
    if not (len(rest) == 1 and rest.isalpha()) and rest not in _CTRL_SYMBOLS:
        raise ValueError(f"unusable prefix key {spec!r}")
    return f"ctrl+{rest}"


def prefix_label(key: str) -> str:
    """``ctrl+right_square_bracket`` → ``Ctrl+]``, for help text and the footer."""
    rest = key[len("ctrl+"):] if key.startswith("ctrl+") else key
    reverse = {name: char for char, name in _PREFIX_ALIASES.items()}
    return f"Ctrl+{reverse.get(rest, rest.upper())}"
