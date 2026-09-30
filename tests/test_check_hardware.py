"""examples/check_hardware.py stays runnable — without touching hardware."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

import pytest

SCRIPT = os.path.join(os.path.dirname(__file__), "..", "examples", "check_hardware.py")


def _load():
    spec = importlib.util.spec_from_file_location("check_hardware", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_help_runs():
    result = subprocess.run([sys.executable, SCRIPT, "--help"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0
    assert "--replug" in result.stdout and "--loopback" in result.stdout


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only script")
def test_a_missing_device_is_refused_before_anything_opens(capsys):
    hw = _load()
    assert hw.preflight("/dev/tty.nothing-here", assume_yes=True) is False
    assert "does not exist" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only script")
def test_without_an_answer_it_does_not_open(tmp_path, monkeypatch, capsys):
    hw = _load()
    node = tmp_path / "node"
    node.write_text("")
    monkeypatch.setattr("uart_proxy.core.port_busy.find_holders", lambda path: [])

    def no_terminal(prompt=""):
        raise EOFError

    monkeypatch.setattr("builtins.input", no_terminal)
    assert hw.preflight(str(node), assume_yes=False) is False
    assert "--yes" in capsys.readouterr().out


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only script")
def test_a_held_port_is_refused_with_its_holder(tmp_path, monkeypatch, capsys):
    from uart_proxy.core.port_busy import Holder

    hw = _load()
    node = tmp_path / "node"
    node.write_text("")
    monkeypatch.setattr("uart_proxy.core.port_busy.find_holders",
                        lambda path: [Holder(4242, "screen")])
    assert hw.preflight(str(node), assume_yes=True) is False
    out = capsys.readouterr().out
    assert "screen (pid 4242)" in out and "Close it first" in out


def test_checks_collect_every_failure():
    hw = _load()
    checks = hw.Checks(verbose=False)
    checks.ok("first", False, "why")
    checks.ok("second", True)
    checks.ok("third", False)
    assert checks.failures == ["first — why", "third"]
