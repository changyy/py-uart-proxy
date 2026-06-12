"""S1: versioning — single source, correct scheme."""

from __future__ import annotations

import re

import uart_proxy


def test_version_scheme():
    # 1.YYYYmmdd.1HHmmss
    assert re.match(r"^1\.\d{8}\.1\d{6}$", uart_proxy.__version__)


def test_version_matches_installed_metadata():
    try:
        from importlib.metadata import version
    except ImportError:  # pragma: no cover
        return
    try:
        installed = version("uart-proxy")
    except Exception:  # pragma: no cover - not installed in this env
        return
    assert installed == uart_proxy.__version__
