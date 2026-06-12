"""S2: time axes."""

from __future__ import annotations

import re

from uart_proxy.core.timestamp import TimestampTracker, format_elapsed


def test_format_elapsed_basic():
    assert format_elapsed(0) == "00:00:00.0000"
    assert format_elapsed(10) == "00:00:10.0000"
    assert format_elapsed(3661.5) == "01:01:01.5000"


def test_format_elapsed_negative_clamped():
    assert format_elapsed(-5) == "00:00:00.0000"


def test_stamp_has_both_axes_and_is_monotonic():
    tracker = TimestampTracker()
    s1 = tracker.stamp()
    s2 = tracker.stamp()
    assert s2.elapsed >= s1.elapsed >= 0
    # wall time derived from start + elapsed -> non-decreasing
    assert s2.wall >= s1.wall
    assert re.match(r"\d{2}:\d{2}:\d{2}\.\d{4}", s1.elapsed_str())


def test_windows_render_both_views():
    tracker = TimestampTracker()
    end = tracker.stamp()
    assert " ~ " in tracker.abs_window(end)
    assert tracker.rel_window(end).startswith("00:00:00.0000 ~ ")
