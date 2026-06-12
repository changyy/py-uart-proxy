"""S4: multi-stream recording."""

from __future__ import annotations

import os

from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.recorder import Recorder
from uart_proxy.core.timestamp import TimestampTracker


def _rx_data(text: str) -> Event:
    raw = text.encode()
    return Event(EventKind.DATA, Direction.RX, TimestampTracker().stamp(), data=raw, text=text)


def _rx_line(text: str) -> Event:
    return Event(EventKind.LINE, Direction.RX, TimestampTracker().stamp(), data=text.encode(), text=text)


def test_three_files_written(tmp_path):
    rec = Recorder(str(tmp_path), base_name="output")
    rec.handle(_rx_data("hello\n"))
    rec.handle(_rx_line("hello"))
    rec.close()

    raw = os.path.join(tmp_path, "output.log")
    rel = os.path.join(tmp_path, "output-timestamp.log")
    full = os.path.join(tmp_path, "output-fulltimestamp.log")
    assert os.path.exists(raw) and os.path.exists(rel) and os.path.exists(full)

    assert open(raw, "rb").read() == b"hello\n"

    rel_text = open(rel, encoding="utf-8").read()
    assert rel_text.endswith("hello\n")
    assert rel_text.startswith("[00:00:")  # relative HH:MM:SS prefix

    full_text = open(full, encoding="utf-8").read()
    assert " | 00:00:" in full_text and full_text.endswith("hello\n")


def test_tx_excluded_by_default_included_when_requested(tmp_path):
    tx_line = Event(EventKind.LINE, Direction.TX, TimestampTracker().stamp(), text="AT")

    rec = Recorder(str(tmp_path), base_name="a", include_tx=False)
    rec.handle(tx_line)
    rec.close()
    assert open(os.path.join(tmp_path, "a-timestamp.log")).read() == ""

    rec = Recorder(str(tmp_path), base_name="b", include_tx=True)
    rec.handle(tx_line)
    rec.close()
    assert ">> AT" in open(os.path.join(tmp_path, "b-timestamp.log")).read()
