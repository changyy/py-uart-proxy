"""The pre-push hook: the tests run before a push (UARTist SPEC D36's twin)."""

import os
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / ".githooks" / "pre-push"


def test_the_pre_push_hook_runs_the_tests_and_blocks_on_failure():
    text = HOOK.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh")
    assert "pytest" in text and "exit 1" in text
    if os.name == "posix":
        assert os.access(HOOK, os.X_OK), "the hook must be executable"
