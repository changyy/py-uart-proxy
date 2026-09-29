"""S20 through the real TUI: character mode swaps the view, not just the input.

The unit tests in test_terminal.py prove the emulation. These prove the wiring —
that device output reaches the screen, that the log keeps its history while the
screen is in front of it, and that switching back shows nothing was lost.
"""

from __future__ import annotations

import asyncio

import pytest

from uart_proxy.core.session import UartSession
from uart_proxy.ui.terminal import PYTE_AVAILABLE
from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE

from conftest import FakeSource

pytestmark = pytest.mark.skipif(
    not (_TEXTUAL_AVAILABLE and PYTE_AVAILABLE),
    reason="textual and pyte are both needed",
)

PREFIX = "ctrl+right_square_bracket"


async def _settle(pilot, tries=8):
    """Let the read thread, the bus and the UI drain timer all catch up."""
    for _ in range(tries):
        await asyncio.sleep(0.03)
        await pilot.pause()


def _run(coro):
    return asyncio.run(coro)


def _app(*, echo=False, **kwargs):
    from uart_proxy.ui.tui import UartProxyApp

    source = FakeSource(echo=echo)
    session = UartSession(source, auto_reconnect=False, default_eol=b"\r")
    return UartProxyApp(session, **kwargs), session, source


def _screen(app) -> list[str]:
    return app._term.emulator.display


def test_character_mode_shows_the_screen_and_line_mode_shows_the_log():
    async def scenario():
        app, session, _ = _app(input_mode="line")
        async with app.run_test() as pilot:
            await _settle(pilot)
            assert app._log.display and not app._term.display

            await pilot.press(PREFIX)
            await pilot.press("c")
            await _settle(pilot)
            assert app._term.display and not app._log.display

            await pilot.press(PREFIX)
            await pilot.press("c")
            await _settle(pilot)
            assert app._log.display and not app._term.display
        session.stop()

    _run(scenario())


def test_echoed_keystrokes_stay_on_one_line():
    """The reported bug, end to end.

    Each keystroke is echoed back by the device as its own RX chunk. The log
    view turned every one of them into a row of its own, because the session
    force-flushes a partial line after a moment's silence — which typing speed
    triggers on every character. On a screen they simply sit side by side.
    """
    async def scenario():
        app, session, source = _app(input_mode="char", echo=True)
        async with app.run_test() as pilot:
            await _settle(pilot)
            for key in "ls":
                await pilot.press(key)
                # A pause longer than the session's 0.2s idle flush, so this
                # fails the way the old view did rather than by accident.
                await asyncio.sleep(0.25)
                await pilot.pause()
            await _settle(pilot)

            assert b"".join(source.writes) == b"ls"
            assert _screen(app)[0].rstrip() == "ls"
            assert _screen(app)[1].strip() == "", "'s' landed on a new row"
        session.stop()

    _run(scenario())


def test_the_device_can_redraw_what_it_already_sent():
    """Tab completion, in the only form a serial line has: backspaces."""
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            source.feed(b"$ cat READ")
            await _settle(pilot)
            source.feed(b"\x08\x08\x08\x08README.md")
            await _settle(pilot)
            assert _screen(app)[0].rstrip() == "$ cat README.md"
        session.stop()

    _run(scenario())


def test_the_screen_is_tracking_before_you_switch_to_it():
    """Switching must reveal the device's screen as it is now. A view that only
    started tracking when you looked at it would open blank."""
    async def scenario():
        app, session, source = _app(input_mode="line")
        async with app.run_test() as pilot:
            await _settle(pilot)
            source.feed(b"arrived while the log was showing")
            await _settle(pilot)
            assert not app._term.display

            await pilot.press(PREFIX)
            await pilot.press("c")
            await _settle(pilot)
            assert _screen(app)[0].rstrip() == "arrived while the log was showing"
        session.stop()

    _run(scenario())


def test_the_log_keeps_its_history_behind_the_screen():
    """The division of labour: the screen is now, the log is everything. So
    switching back has to show what arrived while the screen was in front."""
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            source.feed(b"line one\r\nline two\r\n")
            await _settle(pilot)

            await pilot.press(PREFIX)
            await pilot.press("c")          # back to the log
            await _settle(pilot)
            copied = "\n".join(app._copy_lines)
            assert "line one" in copied and "line two" in copied
        session.stop()

    _run(scenario())


def test_clear_in_character_mode_clears_the_screen_not_the_history():
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            source.feed(b"keep me in the log\r\n")
            await _settle(pilot)

            await pilot.press(PREFIX)
            await pilot.press("k")
            await _settle(pilot)

            assert _screen(app)[0].strip() == ""
            assert "keep me in the log" in "\n".join(app._copy_lines)
        session.stop()

    _run(scenario())


def test_the_screen_matches_the_widget_size():
    """The device is never told the size, so the status bar reports ours — it
    is the number to set over there with `stty`."""
    async def scenario():
        app, session, _ = _app(input_mode="char")
        async with app.run_test(size=(100, 30)) as pilot:
            await _settle(pilot)
            assert app._term.emulator.columns == 100
            assert 1 <= app._term.emulator.lines < 30   # minus header/status/input
            assert app._term.geometry_label.startswith("100×")
        session.stop()

    _run(scenario())


def test_a_note_is_surfaced_when_the_log_is_hidden():
    """Notes are written to the log either way, but in character mode the log
    is behind the screen, so they also have to be raised where they can be
    seen."""
    async def scenario():
        app, session, _ = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("question_mark")     # <prefix> ? — the help block
            await _settle(pilot)
            messages = [n.message for n in app._notifications]
            assert messages, "help was invisible"
            # One toast carrying the whole block, not one per line.
            help_toasts = [m for m in messages
                           if "Everything else goes to the device" in m]
            assert len(help_toasts) == 1
            assert "detach" in help_toasts[0] and "select mode" in help_toasts[0]
        session.stop()

    _run(scenario())
