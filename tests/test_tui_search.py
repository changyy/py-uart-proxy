"""S28: search the log — show only matching lines, live, and back again."""

from __future__ import annotations

import asyncio

import pytest

from uart_proxy.core.session import UartSession
from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE

from conftest import FakeSource

pytestmark = pytest.mark.skipif(not _TEXTUAL_AVAILABLE, reason="textual not installed")

PREFIX = "ctrl+right_square_bracket"


async def _settle(pilot, tries=3):
    for _ in range(tries):
        await asyncio.sleep(0.03)
        await pilot.pause()


def _shown(app) -> list[str]:
    """The text of every row the log widget currently holds."""
    return [strip.text for strip in app._log.lines]


async def _feed(pilot, source, *lines):
    for line in lines:
        source.feed(line.encode() + b"\n")
    await _settle(pilot)


async def _search(pilot, pattern: str):
    await pilot.press(PREFIX)
    await pilot.press("slash")
    await _settle(pilot)
    for ch in pattern:
        await pilot.press(ch if ch != " " else "space")
    await pilot.press("enter")
    await _settle(pilot)


def _scenario(body, **app_kwargs):
    from uart_proxy.ui.tui import UartProxyApp

    async def run():
        source = FakeSource()
        session = UartSession(source, auto_reconnect=False)
        app = UartProxyApp(session, **app_kwargs)
        async with app.run_test(size=(120, 40)) as pilot:
            await _settle(pilot)
            await body(app, pilot, source)
        session.stop()

    asyncio.run(run())


def test_a_filter_shows_only_matching_lines():
    async def body(app, pilot, source):
        await _feed(pilot, source, "boot ok", "ERROR disk", "idle", "ERROR fan")
        await _search(pilot, "error")
        shown = _shown(app)
        assert any("ERROR disk" in row for row in shown)
        assert any("ERROR fan" in row for row in shown)
        assert not any("boot ok" in row or "idle" in row for row in shown)

    _scenario(body)


def test_new_lines_are_filtered_live():
    async def body(app, pilot, source):
        await _feed(pilot, source, "ERROR one")
        await _search(pilot, "error")
        await _feed(pilot, source, "noise", "ERROR two")
        shown = _shown(app)
        assert any("ERROR two" in row for row in shown)
        assert not any("noise" in row for row in shown)

    _scenario(body)


def test_an_empty_search_brings_the_whole_log_back():
    async def body(app, pilot, source):
        await _feed(pilot, source, "boot ok", "ERROR disk")
        await _search(pilot, "error")
        await _feed(pilot, source, "arrived while filtered")
        await _search(pilot, "")
        shown = _shown(app)
        for line in ("boot ok", "ERROR disk", "arrived while filtered"):
            assert any(line in row for row in shown), f"{line!r} lost by filtering"

    _scenario(body)


def test_smart_case():
    async def body(app, pilot, source):
        await _feed(pilot, source, "Error mixed", "error lower")
        await _search(pilot, "error")          # all lower: any case
        assert sum("rror" in row for row in _shown(app)) == 2
        await _search(pilot, "Error")          # a capital: exact case
        shown = _shown(app)
        assert any("Error mixed" in row for row in shown)
        assert not any("error lower" in row for row in shown)

    _scenario(body)


def test_the_status_bar_names_the_filter_and_its_count():
    async def body(app, pilot, source):
        await _feed(pilot, source, "ERROR a", "ok", "ERROR b")
        await _search(pilot, "error")
        app._refresh_status()
        status = str(getattr(app._status, "content", None)
                     or getattr(app._status, "renderable", ""))
        assert "filter 'error' (2)" in status

    _scenario(body)


def test_copy_takes_what_is_shown(monkeypatch):
    async def body(app, pilot, source):
        copied = []
        monkeypatch.setattr(app, "_copy_text", lambda text: copied.append(text) or "test")
        await _feed(pilot, source, "ERROR a", "ok", "ERROR b")
        await _search(pilot, "error")
        app.action_copy_all()
        assert "ERROR a" in copied[0] and "ERROR b" in copied[0]
        assert "ok" not in copied[0].splitlines()[-1] and "< ok" not in copied[0]

    _scenario(body)


def test_escape_cancels_without_changing_the_filter():
    async def body(app, pilot, source):
        await _feed(pilot, source, "ERROR a", "ok")
        await _search(pilot, "error")
        await pilot.press(PREFIX)
        await pilot.press("slash")
        await _settle(pilot)
        await pilot.press("x")
        await pilot.press("escape")
        await _settle(pilot)
        assert app._filter == "error"
        assert not app.query_one("#find").display
        # …and typing goes back to the device input, not the search box.
        assert app.focused is app.query_one("#cmd")

    _scenario(body)


def test_reopening_the_search_names_the_current_pattern_but_starts_empty():
    """Empty, so that an empty Enter clears without deleting anything first."""
    async def body(app, pilot, source):
        await _search(pilot, "error")
        await pilot.press(PREFIX)
        await pilot.press("slash")
        await _settle(pilot)
        find = app.query_one("#find")
        assert find.display and find.value == ""
        assert "'error'" in find.placeholder and "clear" in find.placeholder
        assert app.focused is find

    _scenario(body)


def test_typing_in_the_search_box_is_not_sent_to_the_device():
    async def body(app, pilot, source):
        await _search(pilot, "error")
        await _settle(pilot)
        assert source.writes == []

    _scenario(body)


def test_markup_like_text_is_matched_literally():
    """A device line with `[red]` in it is text, not markup, when filtered."""
    async def body(app, pilot, source):
        await _feed(pilot, source, "value [red] here", "other")
        await _search(pilot, "[red]")
        shown = _shown(app)
        assert any("value [red] here" in row for row in shown)

    _scenario(body)


def test_in_character_mode_it_explains_instead():
    async def body(app, pilot, source):
        await pilot.press(PREFIX)
        await pilot.press("slash")
        await _settle(pilot)
        assert not app.query_one("#find").display
        assert any("switch to line mode" in n.message for n in app._notifications)
        assert source.writes == [], "the / must not reach the device either"

    _scenario(body, input_mode="char")


def test_clear_keeps_the_filter_for_what_comes_next():
    async def body(app, pilot, source):
        await _feed(pilot, source, "ERROR old")
        await _search(pilot, "error")
        app.action_clear_log()
        await _feed(pilot, source, "noise", "ERROR new")
        shown = _shown(app)
        assert any("ERROR new" in row for row in shown)
        assert not any("ERROR old" in row or "noise" in row for row in shown)

    _scenario(body)


def test_the_help_lists_search():
    async def body(app, pilot, source):
        await pilot.press(PREFIX)
        await pilot.press("question_mark")
        await _settle(pilot)
        assert any("search" in line for line in app._copy_lines)

    _scenario(body)
