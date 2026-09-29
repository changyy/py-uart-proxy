"""S20: the terminal emulation behind character mode.

These are about the things a log view structurally cannot do. The first test is
the bug that prompted the whole feature: at typing speed every echoed keystroke
was force-flushed as its own "line", so ``ls`` arrived on screen as two rows.
"""

from __future__ import annotations

import pytest

from uart_proxy.ui.terminal import PYTE_AVAILABLE, TerminalEmulator, rich_color

pytestmark = pytest.mark.skipif(not PYTE_AVAILABLE, reason="pyte not installed")


def _term(columns: int = 40, lines: int = 5) -> TerminalEmulator:
    return TerminalEmulator(columns, lines)


def test_echoed_keystrokes_accumulate_on_one_line():
    """The regression. Each keystroke arrives as its own RX chunk, seconds
    apart; they must land side by side, not one per row."""
    term = _term()
    for char in b"ls -l":
        term.feed(bytes([char]))
    assert term.display[0].rstrip() == "ls -l"
    assert term.display[1].strip() == "", "the second row should still be empty"


def test_backspace_edits_the_line_in_place():
    """What tab-completion and a typo correction both rely on."""
    term = _term()
    term.feed(b"ls doc")
    term.feed(b"\x08\x08\x08uments")
    assert term.display[0].rstrip() == "ls uments"


def test_carriage_return_redraws_from_the_start_of_the_line():
    """A progress bar sends CR without LF and repaints the same row."""
    term = _term()
    term.feed(b"downloading  10%")
    term.feed(b"\rdownloading 100%")
    assert term.display[0].rstrip() == "downloading 100%"


def test_a_shell_can_repaint_the_whole_screen():
    """ANSI cursor addressing + erase — what vi, htop and `clear` all use."""
    term = _term()
    term.feed(b"old output\r\nmore old output")
    term.feed(b"\x1b[2J\x1b[H" + b"fresh")
    assert term.display[0].rstrip() == "fresh"
    assert term.display[1].strip() == ""


def test_colour_reaches_the_rendered_text():
    term = _term()
    term.feed(b"\x1b[31mERROR\x1b[0m ok")
    text = term.render()
    colours = {
        span.style.color.name
        for span in text.spans
        if span.style is not None and span.style.color is not None
    }
    assert "red" in colours


def test_pytes_colour_names_are_translated_for_rich():
    """pyte and Rich disagree about colour names, and pyte has a typo.

    ``brown`` is ANSI 33 (which Rich calls yellow and refuses as "brown"), the
    bright variants lack Rich's underscore, and ``bfightmagenta`` is pyte
    0.8.2's own misspelling of BG 105. Any of them reaching Rich unmapped is a
    crash on a device that merely printed in colour.
    """
    from rich.color import Color

    import pyte.graphics as graphics

    assert rich_color("default") is None
    assert rich_color("brown") == "yellow"
    assert rich_color("brightbrown") == "bright_yellow"
    assert rich_color("bfightmagenta") == "bright_magenta"
    assert rich_color("ff8700") == "#ff8700"      # 256-colour comes as bare hex

    # Every colour pyte can actually emit must be one Rich accepts.
    for table in (graphics.FG, graphics.BG,
                  graphics.FG_AIXTERM, graphics.BG_AIXTERM):
        for name in table.values():
            translated = rich_color(name)
            if translated is not None:
                Color.parse(translated)  # raises if Rich doesn't know it


def test_a_double_width_character_does_not_shift_the_row():
    """pyte stores the trailing cell of a wide character as an empty string;
    padding it with a space would push the rest of the row one column right."""
    term = _term()
    term.feed("中文 ok".encode("utf-8"))
    text = term.render()
    assert text.plain.split("\n")[0].rstrip() == "中文 ok"


def test_resizing_keeps_what_is_on_screen():
    term = _term(40, 5)
    term.feed(b"still here")
    term.resize(60, 10)
    assert term.columns == 60 and term.lines == 10
    assert term.display[0].rstrip() == "still here"


def test_rendering_is_skipped_when_nothing_moved():
    """A quiet device must not cost a redraw 20 times a second."""
    term = _term()
    term.feed(b"hello")
    assert term.dirty
    term.render()
    assert not term.dirty
    term.feed(b"!")
    assert term.dirty


def test_the_cursor_moving_alone_still_counts_as_dirty():
    """Moving the cursor does not necessarily dirty a line, but a cursor drawn
    in the wrong place is very visible."""
    term = _term()
    term.feed(b"abc")
    term.render()
    term.feed(b"\x1b[1;1H")   # home, no text changed
    assert term.dirty


def test_malformed_input_does_not_raise():
    """A device at the wrong baud rate produces garbage; that is a reason to
    show garbage, not to take the UI down."""
    term = _term()
    term.feed(b"\x1b[999999999999;9999999999H\x1b[")
    term.feed(b"\xff\xfe\x00 still alive")
    term.render()


def test_reset_clears_the_screen():
    term = _term()
    term.feed(b"gone")
    term.reset()
    assert term.display[0].strip() == ""


def test_shrinking_drops_rows_from_the_top():
    """Documents the pyte behaviour the TUI has to work around.

    A screen that shrinks loses its *first* lines, not its last. That is what a
    real terminal does, and it is why the TUI keeps the hidden screen sized to
    the region it will be drawn in: a screen squashed while nobody was looking
    would come back with its opening rows gone.
    """
    term = _term(40, 10)
    term.feed(b"first\r\nsecond\r\nthird\r\nfourth\r\nfifth\r\nsixth")
    term.resize(40, 5)
    rows = "\n".join(term.display)
    assert "first" not in rows, "shrinking should clip the top, and did not"
    assert "sixth" in rows, "the bottom of the screen should survive"
