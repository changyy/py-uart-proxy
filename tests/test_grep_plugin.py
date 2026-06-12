"""S7: built-in grep plugin and manager dispatch."""

from __future__ import annotations

import time

from uart_proxy.core.events import EventKind
from uart_proxy.core.session import UartSession
from uart_proxy.plugins.manager import PluginManager

from conftest import FakeSource


def _wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def test_grep_emits_notice_on_match_only():
    source = FakeSource()
    session = UartSession(source)
    notices = []
    session.bus.subscribe(
        lambda e: notices.append(e.text) if e.kind == EventKind.NOTICE else None
    )

    manager = PluginManager(session)
    manager.add_builtin("grep", {"patterns": ["ERROR"]})
    manager.start()

    session.start()
    source.feed(b"all good\n")
    source.feed(b"ERROR: disk full\n")

    assert _wait_for(lambda: any("ERROR: disk full" in n for n in notices))
    session.stop()
    manager.stop()

    match_notices = [n for n in notices if n.startswith("grep[ERROR]")]
    assert len(match_notices) == 1
    assert "all good" not in " ".join(match_notices)
