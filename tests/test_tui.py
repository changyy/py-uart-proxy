"""
S5/S10: TUI rendering, input focus/sending, and mouse follow-tail.

These reproduce the reported bug ("can't see messages, can't send") and lock in
the fix: events are drained onto the UI thread via a timer (no call_from_thread
from the UI thread), and the input box is focused on mount.

Uses Textual's headless ``run_test`` harness driven with ``asyncio.run`` so no
pytest-asyncio plugin is required.
"""

from __future__ import annotations

import asyncio
import sys

import pytest

from uart_proxy.core.session import UartSession
from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE

from conftest import FakeSource

pytestmark = pytest.mark.skipif(not _TEXTUAL_AVAILABLE, reason="textual not installed")


async def _settle(pilot, predicate, tries=20):
    for _ in range(tries):
        await asyncio.sleep(0.05)
        await pilot.pause()
        if predicate():
            return True
    return False


def test_tui_renders_incoming_rx():
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        src = FakeSource()
        session = UartSession(src)
        app = UartProxyApp(session, title="t")
        async with app.run_test(size=(80, 24)) as pilot:
            log = app.query_one("#log")
            await _settle(pilot, lambda: True, tries=4)  # let mount/connect settle
            base = len(log.lines)
            src.feed(b"hello world\n")
            assert await _settle(pilot, lambda: len(log.lines) > base)

    asyncio.run(scenario())


def test_tui_focuses_input_and_sends():
    from textual.widgets import Input

    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        src = FakeSource()
        session = UartSession(src, default_eol=b"\r\n")
        app = UartProxyApp(session, title="t")
        async with app.run_test(size=(80, 24)) as pilot:
            await _settle(pilot, lambda: session.is_connected)
            # The input box — not the scroll log — must own focus on mount.
            assert app.focused is app.query_one("#cmd", Input)
            await pilot.press("a", "t")
            await pilot.press("enter")
            assert await _settle(pilot, lambda: b"at\r\n" in b"".join(src.writes))

    asyncio.run(scenario())


def test_tui_log_supports_selection_and_copy():
    # S13: the log is selectable and the app exposes clipboard copy.
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        session = UartSession(FakeSource())
        app = UartProxyApp(session, title="t")
        async with app.run_test(size=(80, 24)) as pilot:
            log = app.query_one("#log")
            assert log.allow_select is True
            # Built-in copy binding exists and the API is callable.
            assert hasattr(app.screen, "action_copy_text")
            app.copy_to_clipboard("hello")
            await pilot.pause()

    asyncio.run(scenario())


def test_tui_copy_all_is_clean_text():
    # S13: Ctrl+W copies the log as clean text — no border chars, no padding.
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        source = FakeSource()
        session = UartSession(source)
        app = UartProxyApp(session, title="t")
        async with app.run_test(size=(80, 16)) as pilot:
            await _settle(pilot, lambda: session.is_connected)
            source.feed(b"hello world\nsecond line\n")
            assert await _settle(
                pilot, lambda: any("hello world" in s for s in app._copy_lines)
            )
            await pilot.press("ctrl+w")
            await pilot.pause()
            clip = app.clipboard
            assert "hello world" in clip and "second line" in clip
            # clean: no box-drawing border, no trailing-pad run of spaces
            assert "│" not in clip
            assert "   " not in clip

    asyncio.run(scenario())


def test_native_clipboard_cmd():
    import shutil as _shutil

    from uart_proxy.ui.tui import _native_clipboard_cmd

    cmd = _native_clipboard_cmd()
    if sys.platform == "darwin" and _shutil.which("pbcopy"):
        assert cmd == ["pbcopy"]


def test_tui_clear_resets_copy_range():
    # Ctrl+K clears both the display and the buffer that Ctrl+W copies.
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        source = FakeSource()
        session = UartSession(source)
        app = UartProxyApp(session, title="t")
        async with app.run_test(size=(80, 16)) as pilot:
            await _settle(pilot, lambda: session.is_connected)
            source.feed(b"old line one\nold line two\n")
            assert await _settle(pilot, lambda: any("old line" in s for s in app._copy_lines))

            await pilot.press("ctrl+k")
            await pilot.pause()
            assert len(app._copy_lines) == 0  # copy range reset

            source.feed(b"fresh line\n")
            assert await _settle(pilot, lambda: any("fresh line" in s for s in app._copy_lines))
            # only the new range remains
            assert not any("old line" in s for s in app._copy_lines)

    asyncio.run(scenario())


def test_tui_select_mode_toggle():
    # S13: Ctrl+E enters select mode (freezes follow, hands mouse to terminal);
    # Ctrl+E again restores following.
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        session = UartSession(FakeSource())
        app = UartProxyApp(session, title="t")
        async with app.run_test(size=(80, 24)) as pilot:
            log = app.query_one("#log")
            assert app._select_mode is False
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert app._select_mode is True
            assert log.auto_scroll is False  # frozen so selection isn't disturbed
            await pilot.press("ctrl+e")
            await pilot.pause()
            assert app._select_mode is False
            assert log.auto_scroll is True   # follow restored

    asyncio.run(scenario())


def test_tui_mouse_scroll_pauses_and_resumes_follow():
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        src = FakeSource()
        session = UartSession(src)
        app = UartProxyApp(session, title="t")
        async with app.run_test(size=(80, 24)) as pilot:
            log = app.query_one("#log")
            await pilot.pause()
            assert log.following is True          # follows the tail by default
            log.on_mouse_scroll_up(None)          # user scrolls up to read history
            await pilot.pause()
            assert log.following is False          # auto-follow paused
            log.jump_to_bottom()                   # End key / back to bottom
            await pilot.pause()
            assert log.following is True           # resumed

    asyncio.run(scenario())
