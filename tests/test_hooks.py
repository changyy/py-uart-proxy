"""The pre-push hook: the tests, under every Python CI runs, before a push."""

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = ROOT / ".githooks" / "pre-push"
SCRIPT = ROOT / "scripts" / "test-pythons.sh"


def test_the_pre_push_hook_runs_every_python_and_blocks_on_failure():
    text = HOOK.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh") and "scripts/test-pythons.sh" in text and "exit 1" in text
    if os.name == "posix":
        assert os.access(HOOK, os.X_OK) and os.access(SCRIPT, os.X_OK), "both must be executable"


def test_the_local_run_covers_the_same_pythons_as_ci():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    ci_versions = re.search(r"python-version: \[([^\]]+)\]", ci).group(1)
    ci_set = {v.strip().strip('"') for v in ci_versions.split(",")}
    script = SCRIPT.read_text(encoding="utf-8")
    local = set(re.search(r'VERSIONS="\$\{\*:-([^}]+)\}"', script).group(1).split())
    assert local == ci_set, f"pre-push runs {sorted(local)}, CI runs {sorted(ci_set)}"
