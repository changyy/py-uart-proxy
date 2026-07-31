"""S19: the command prefix and character mode, driven through Textual.

The point of these is the thing a unit test cannot show: that the keys really
arrive, that the app survives ``^C`` instead of quitting on it, and that a
printable key reaches the wire — which only works because character mode moves
focus off the Input widget (a focused Input swallows printables).
"""

from __future__ import annotations

import asyncio

import pytest

from uart_proxy.core.session import UartSession
from uart_proxy.ui.keymap import DEFAULT_PREFIX
from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE

from conftest import FakeSource

pytestmark = pytest.mark.skipif(not _TEXTUAL_AVAILABLE, reason="textual not installed")

PREFIX = "ctrl+right_square_bracket"


async def _settle(pilot, tries=4):
    for _ in range(tries):
        await asyncio.sleep(0.02)
        await pilot.pause()


def _run(coro):
    return asyncio.run(coro)


def _app(**kwargs):
    from uart_proxy.ui.tui import UartProxyApp

    source = FakeSource()
    session = UartSession(source, auto_reconnect=False, default_eol=b"\r")
    return UartProxyApp(session, **kwargs), session, source


def test_a_printable_key_is_sent_in_char_mode():
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press("a")
            await pilot.press("b")
            await _settle(pilot)
            assert b"".join(source.writes) == b"ab"
        session.stop()

    _run(scenario())


def test_ctrl_c_reaches_the_device_and_does_not_quit():
    """The whole reason character mode exists. Textual binds ctrl+c to quit, so
    this also pins that we intercept it."""
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press("ctrl+c")
            await _settle(pilot)
            assert b"\x03" in b"".join(source.writes)
            assert app.is_running, "ctrl+c quit the app instead of going to the wire"
            await pilot.press("x")          # still alive and still forwarding
            await _settle(pilot)
            assert b"".join(source.writes).endswith(b"x")
        session.stop()

    _run(scenario())


def test_ctrl_d_and_arrows_reach_the_device():
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press("ctrl+d")
            await pilot.press("up")
            await _settle(pilot)
            sent = b"".join(source.writes)
            assert b"\x04" in sent and b"\x1b[A" in sent
        session.stop()

    _run(scenario())


def test_line_mode_does_not_forward_keystrokes():
    """In line mode the keys belong to the input box until Enter."""
    async def scenario():
        app, session, source = _app(input_mode="line")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press("a", "b", "c")
            await _settle(pilot)
            assert source.writes == []
            await pilot.press("enter")
            await _settle(pilot)
            assert b"".join(source.writes) == b"abc\r"
        session.stop()

    _run(scenario())


def test_the_prefix_switches_between_the_modes():
    async def scenario():
        app, session, source = _app(input_mode="line")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("c")
            await _settle(pilot)
            assert app._char_mode is True
            await pilot.press("z")
            await _settle(pilot)
            assert b"z" in b"".join(source.writes)

            await pilot.press(PREFIX)
            await pilot.press("c")
            await _settle(pilot)
            assert app._char_mode is False
            before = len(source.writes)
            await pilot.press("q")           # goes to the input box now
            await _settle(pilot)
            assert len(source.writes) == before
        session.stop()

    _run(scenario())


def test_pressing_the_prefix_twice_sends_it_literally():
    """Without this, a device that wants the prefix byte could never receive it,
    which would make the choice of prefix a dead end."""
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press(PREFIX)
            await _settle(pilot)
            assert b"\x1d" in b"".join(source.writes)
        session.stop()

    _run(scenario())


def test_the_prefix_is_not_sent_on_its_own():
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await _settle(pilot)
            assert source.writes == [], "the prefix leaked to the device"
            assert app._awaiting_command is True
        session.stop()

    _run(scenario())


def test_a_prefix_command_is_not_sent_to_the_device_either():
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("t")            # cycle timestamps
            await _settle(pilot)
            assert source.writes == []
            assert app._awaiting_command is False
        session.stop()

    _run(scenario())


def test_an_unknown_command_says_so_instead_of_sending_it():
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("j")
            await _settle(pilot)
            assert source.writes == []
            assert any("unknown" in line for line in app._copy_lines)
        session.stop()

    _run(scenario())


def test_detach_leaves_the_session_running_when_there_is_one():
    async def scenario():
        app, session, source = _app(detachable=True)
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("d")
            await _settle(pilot)
        assert app._detached is True
        session.stop()

    _run(scenario())


def test_detach_explains_itself_when_there_is_nothing_to_detach_from():
    """A foreground `connect` has no daemon behind it, so detaching would just
    kill the session — say that instead of pretending."""
    async def scenario():
        app, session, source = _app(detachable=False)
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press(PREFIX)
            await pilot.press("d")
            await _settle(pilot)
            assert app._detached is False
            assert app.is_running
            assert any("nothing to detach" in line for line in app._copy_lines)
        session.stop()

    _run(scenario())


def test_a_custom_prefix_is_honoured():
    async def scenario():
        app, session, source = _app(input_mode="char", prefix="ctrl+a")
        async with app.run_test() as pilot:
            await _settle(pilot)
            await pilot.press("ctrl+a")       # the prefix now, not INTR-adjacent
            await pilot.press("c")
            await _settle(pilot)
            assert app._char_mode is False, "custom prefix did not take effect"
            assert source.writes == []
        session.stop()

    _run(scenario())


def test_char_mode_gives_the_apps_own_ctrl_keys_to_the_device():
    """Ctrl+W is kill-word in a shell. The app's binding for it is priority=True,
    so without check_action it would be swallowed before on_key — stealing keys
    from the device is precisely what character mode exists to stop."""
    async def scenario():
        app, session, source = _app(input_mode="char")
        async with app.run_test() as pilot:
            await _settle(pilot)
            for key in ("ctrl+w", "ctrl+t", "ctrl+y", "ctrl+k", "ctrl+e"):
                await pilot.press(key)
            await _settle(pilot)
            sent = b"".join(source.writes)
            assert sent == b"\x17\x14\x19\x0b\x05", f"got {sent!r}"
        session.stop()

    _run(scenario())


def test_line_mode_keeps_the_familiar_shortcuts():
    """They don't conflict there — line mode never forwards keystrokes — so the
    muscle memory keeps working."""
    async def scenario():
        app, session, source = _app(input_mode="line")
        async with app.run_test() as pilot:
            await _settle(pilot)
            before = _TS_INDEX = app._ts_index
            await pilot.press("ctrl+t")
            await _settle(pilot)
            assert app._ts_index != before, "Ctrl+T stopped cycling timestamps"
            assert source.writes == []
        session.stop()

    _run(scenario())


def test_the_default_prefix_is_the_documented_one():
    app, session, _ = _app()
    assert app._prefix == DEFAULT_PREFIX
    assert app._prefix_label == "Ctrl+]"
