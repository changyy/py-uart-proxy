"""Retention policy: age-based and size-based pruning of the session store."""

from __future__ import annotations

import os
import time

from uart_proxy.core.retention import (
    apply_retention,
    dir_size,
    format_bytes,
    scan_sessions,
)


def _make_session(root: str, name: str, *, size: int, age_days: float) -> str:
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "output.log"), "wb") as fh:
        fh.write(b"x" * size)
    when = time.time() - age_days * 86400
    os.utime(path, (when, when))
    return path


def test_dir_size_and_format():
    assert format_bytes(0) == "0 B"
    assert format_bytes(1536) == "1.5 KB"
    assert format_bytes(500 * 1024 * 1024) == "500.0 MB"


def test_age_based_pruning(tmp_path):
    root = str(tmp_path)
    old = _make_session(root, "old", size=10, age_days=40)
    young = _make_session(root, "young", size=10, age_days=5)

    report = apply_retention(root, max_age_days=30, max_total_bytes=0)
    assert old in report.deleted
    assert young not in report.deleted
    assert not os.path.exists(old)
    assert os.path.exists(young)


def test_size_based_pruning_deletes_oldest_first(tmp_path):
    root = str(tmp_path)
    # three 100-byte sessions, cap at 250 bytes -> oldest must go
    a = _make_session(root, "a", size=100, age_days=3)  # oldest
    b = _make_session(root, "b", size=100, age_days=2)
    c = _make_session(root, "c", size=100, age_days=1)  # newest

    report = apply_retention(root, max_age_days=0, max_total_bytes=250)
    assert a in report.deleted
    assert os.path.exists(b) and os.path.exists(c)
    assert report.total_after <= 250


def test_protect_keeps_active_session(tmp_path):
    root = str(tmp_path)
    active = _make_session(root, "active", size=100, age_days=99)
    report = apply_retention(root, max_age_days=30, max_total_bytes=0, protect=[active])
    assert active not in report.deleted
    assert os.path.exists(active)


def test_disabled_axes_delete_nothing(tmp_path):
    root = str(tmp_path)
    _make_session(root, "old", size=10_000, age_days=999)
    report = apply_retention(root, max_age_days=0, max_total_bytes=0)
    assert report.deleted == []
    assert len(scan_sessions(root)) == 1
