"""
Terminal emulation for character mode (SPEC S20).

Character mode fixed the *input* half of an interactive session — every key goes
straight to the device. This module fixes the *output* half, which needs a
different kind of surface entirely.

The log view is a list of finished lines: it can only ever append, and a line is
final once written. That is exactly right for reading a device's output after the
fact, and exactly wrong while a shell is on the far end, because a shell talks to
a *screen*: it echoes a character where the cursor is, backs it out with
``BS``, redraws the line from column 0 with ``CR``, repaints a region with an
ANSI sequence. None of those can be expressed by appending a row.

Worse, the two models fight: the session force-flushes a partial RX line after
``_IDLE_FLUSH`` seconds so prompts like ``login: `` appear at all, and at typing
speed each echoed keystroke clears that timer on its own — so every character
became its own "line".

So character mode renders through a real terminal emulator (:mod:`pyte`): device
bytes are fed to a screen buffer, and the buffer is drawn. The division of labour
is then clean, and both views stay live off the same bus:

    RX bytes ─┬─> LineAssembler ──> LINE events ──> log view, recorder,
              │                                     plugins, proxy clients
              └─> pyte.Screen ────────────────────> terminal view

* **terminal view** — what the device's screen looks like *now*, with colour,
  cursor and in-place redraws. No history: it is a screen, not a log.
* **log view** — every line that ever arrived, timestamped. That is where history
  lives, and ``<prefix> c`` switches between them at any time.

Two deliberate omissions:

* **TX is not echoed into the screen.** The far end echoes what you type, which is
  why your own keystrokes appear at all; drawing them locally as well would
  double every character, and would wrongly show input that a device with echo
  off (a password prompt) is deliberately hiding.
* **The device is never told the window size.** RS-232 has no ``SIGWINCH`` — there
  is no in-band way to send one, so the far end keeps whatever it assumed, and a
  full-screen program will paint to *that* size. ``screen`` over serial has the
  same limitation; the fix is on the device (``stty rows 34 cols 135``).
"""

from __future__ import annotations

from typing import Optional

from rich.style import Style
from rich.text import Text

try:
    import pyte

    PYTE_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without pyte
    PYTE_AVAILABLE = False


DEFAULT_COLUMNS = 80
DEFAULT_LINES = 24

#: pyte's colour names are not Rich's, and one of them is a typo in pyte itself.
#:
#: pyte follows the ANSI naming where 33 is "brown"; Rich (and everyone else)
#: calls it yellow, and rejects "brown" outright. The bright variants have no
#: separator where Rich wants an underscore. ``bfightmagenta`` is pyte 0.8.2's
#: own misspelling in ``BG_AIXTERM[105]`` — mapping it here is what keeps a
#: bright-magenta background from raising instead of rendering.
_COLOR_NAMES = {
    "brown": "yellow",
    "brightblack": "bright_black",
    "brightred": "bright_red",
    "brightgreen": "bright_green",
    "brightbrown": "bright_yellow",
    "brightblue": "bright_blue",
    "brightmagenta": "bright_magenta",
    "bfightmagenta": "bright_magenta",
    "brightcyan": "bright_cyan",
    "brightwhite": "bright_white",
}

_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")

#: Drawn as reverse video, the way a block cursor looks in any terminal.
_CURSOR_STYLE = Style(reverse=True)


def rich_color(value: str) -> Optional[str]:
    """Translate one pyte colour into a Rich one, or None for "leave it alone".

    pyte reports either a name, a bare six-digit hex string (256-colour and
    true-colour both land here), or ``"default"``.
    """
    if not value or value == "default":
        return None
    mapped = _COLOR_NAMES.get(value)
    if mapped is not None:
        return mapped
    if len(value) == 6 and all(char in _HEX_DIGITS for char in value):
        return f"#{value}"
    return value


class TerminalEmulator:
    """A screen the device draws on, and the Rich renderable for it.

    Deliberately free of any UI framework so the emulation can be tested on its
    own: feed bytes, read :attr:`display`. The Textual widget in
    :mod:`uart_proxy.ui.tui` is a thin wrapper over this.
    """

    def __init__(self, columns: int = DEFAULT_COLUMNS, lines: int = DEFAULT_LINES) -> None:
        if not PYTE_AVAILABLE:  # pragma: no cover - guarded by the caller
            raise RuntimeError(
                "pyte is not installed. Install it with:  pip install pyte"
            )
        self._screen = pyte.Screen(max(1, columns), max(1, lines))
        self._stream = pyte.ByteStream(self._screen)
        self._style_cache: dict[tuple, Style] = {}
        # Rendering is skipped unless something moved, so a quiet device costs
        # nothing. The cursor is tracked separately because moving it does not
        # necessarily dirty a line, and a cursor that lags is very visible.
        self._drawn_cursor: Optional[tuple] = None

    # ── geometry ────────────────────────────────────────────────────────────

    @property
    def columns(self) -> int:
        return self._screen.columns

    @property
    def lines(self) -> int:
        return self._screen.lines

    def resize(self, columns: int, lines: int) -> None:
        """Match a new widget size. A no-op if nothing changed.

        pyte keeps the contents and clips or pads them, which is the best that
        can be done: the device was never told, so it will carry on drawing to
        the size it still believes in until something on that end changes it.
        """
        columns, lines = max(1, columns), max(1, lines)
        if (columns, lines) == (self._screen.columns, self._screen.lines):
            return
        self._screen.resize(lines, columns)
        self._drawn_cursor = None  # force a redraw at the new size

    # ── input ───────────────────────────────────────────────────────────────

    def feed(self, data: bytes) -> None:
        """Draw device output onto the screen.

        Never raises: a malformed escape sequence from a device at the wrong baud
        rate is not a reason to take the UI down, and pyte can throw on input
        that a real terminal would simply ignore.
        """
        try:
            self._stream.feed(data)
        except Exception:  # noqa: BLE001 - garbage in, previous screen out
            pass

    def reset(self) -> None:
        """Clear the screen, as ``clear`` on the device would."""
        self._screen.reset()
        self._drawn_cursor = None

    # ── output ──────────────────────────────────────────────────────────────

    @property
    def dirty(self) -> bool:
        """Whether anything has changed since the last :meth:`render`."""
        return bool(self._screen.dirty) or self._cursor_key() != self._drawn_cursor

    @property
    def display(self) -> list[str]:
        """The screen as plain text, one string per row."""
        return self._screen.display

    def render(self) -> Text:
        """The screen as a Rich renderable, and mark everything clean."""
        screen = self._screen
        cursor_visible = not screen.cursor.hidden
        cursor_x, cursor_y = screen.cursor.x, screen.cursor.y

        text = Text(no_wrap=True, end="")
        for y in range(screen.lines):
            if y:
                text.append("\n")
            row = screen.buffer[y]
            # Cells are emitted in runs of identical style rather than one span
            # each: a full screen is thousands of cells and almost all of them
            # share a style with their neighbour.
            run: list[str] = []
            run_style: Optional[Style] = None
            for x in range(screen.columns):
                char = row[x]
                style = self._style_for(char)
                if cursor_visible and y == cursor_y and x == cursor_x:
                    style = style + _CURSOR_STYLE
                if style is not run_style:
                    if run:
                        text.append("".join(run), run_style)
                    run = []
                    run_style = style
                # Written verbatim, never padded: the trailing cell of a
                # double-width character is stored as an empty string, and
                # substituting a space for it would shift the rest of the row.
                run.append(char.data)
            if run:
                text.append("".join(run), run_style)

        screen.dirty.clear()
        self._drawn_cursor = self._cursor_key()
        return text

    # ── helpers ─────────────────────────────────────────────────────────────

    def _cursor_key(self) -> tuple:
        cursor = self._screen.cursor
        return (cursor.x, cursor.y, cursor.hidden)

    def _style_for(self, char) -> Style:
        """The Rich style for one cell, cached by its attributes.

        Identity of the cached objects is what lets :meth:`render` group runs
        with an ``is`` check instead of comparing styles.
        """
        key = tuple(char)[1:]  # everything but the character itself
        style = self._style_cache.get(key)
        if style is None:
            (fg, bg, bold, italics, underscore,
             strikethrough, reverse, blink) = key
            style = Style(
                color=rich_color(fg),
                bgcolor=rich_color(bg),
                # `or None` so an unset attribute stays unset rather than
                # actively turning the feature off further down a style stack.
                bold=bold or None,
                italic=italics or None,
                underline=underscore or None,
                strike=strikethrough or None,
                reverse=reverse or None,
                blink=blink or None,
            )
            self._style_cache[key] = style
        return style
