"""The TUI status bar: PTY mirrors (S14) and what a lagging reader has lost."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from uart_proxy.core.session import UartSession
from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE

from conftest import FakeSource

pytestmark = pytest.mark.skipif(not _TEXTUAL_AVAILABLE, reason="textual not installed")


async def _settle(pilot, tries=4):
    for _ in range(tries):
        await asyncio.sleep(0.02)
        await pilot.pause()


def _status_text(app) -> str:
    widget = app._status
    return str(getattr(widget, "content", None) or getattr(widget, "renderable", ""))


def _mirror(dropped=0, stale=0):
    return SimpleNamespace(name="m", link="/tmp/m", dropped=dropped, stale=stale)


def _status_with(mirror_stats) -> str:
    from uart_proxy.ui.tui import UartProxyApp

    async def scenario():
        session = UartSession(FakeSource(), auto_reconnect=False)
        app = UartProxyApp(session, mirror_stats=mirror_stats)
        async with app.run_test() as pilot:
            await _settle(pilot)
            app._refresh_status()
            text = _status_text(app)
        session.stop()
        return text

    return asyncio.run(scenario())


def test_the_mirror_count_is_shown():
    assert "mirrors 2" in _status_with(lambda: [_mirror(), _mirror()])


def test_a_lagging_reader_is_flagged_with_what_it_lost():
    text = _status_with(lambda: [_mirror(dropped=1536), _mirror(dropped=512)])
    assert "dropped 2.0 KB" in text


def test_unread_output_on_an_idle_mirror_is_not_an_alarm():
    """`stale` is what nobody was attached to read — the normal idle state."""
    text = _status_with(lambda: [_mirror(stale=10_000)])
    assert "mirrors 1" in text and "dropped" not in text


def test_no_mirrors_means_no_mirror_label():
    assert "mirrors" not in _status_with(None)


def test_a_failing_stats_call_never_breaks_the_status_bar():
    def boom():
        raise RuntimeError("group already stopped")

    text = _status_with(boom)
    assert "mirrors" not in text and "elapsed" in text
