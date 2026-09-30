"""S27: `connect` without `--port` lets you choose — only where someone can."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from uart_proxy import cli
from uart_proxy.cli import port_choices, resolve_port
from uart_proxy.core import daemon as daemon_mod
from uart_proxy.core.daemon import DAEMON_SUPPORTED, DaemonInfo
from uart_proxy.ui.port_picker import PortChoice, format_choice
from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv(daemon_mod.HOME_ENV, str(tmp_path / "home"))


def _ident(path, *, vid=0x067B, desc="USB-Serial Controller"):
    return SimpleNamespace(tty_device=path, vid=vid, vid_pid_str="067B:23A3",
                           description=desc)


PORTS = [_ident("/dev/tty.PL2303G-USBtoUART110"),
         _ident("/dev/tty.Bluetooth-Incoming-Port", vid=None, desc="")]


def _args(port=None, no_tui=False):
    return argparse.Namespace(port=port, no_tui=no_tui)


@pytest.fixture
def terminal(monkeypatch):
    monkeypatch.setattr(cli, "_have_terminal", lambda: True)


@pytest.fixture
def no_terminal(monkeypatch):
    monkeypatch.setattr(cli, "_have_terminal", lambda: False)


# ── describing the ports ────────────────────────────────────────────────────


def test_each_port_is_described_by_what_identifies_it():
    choices = port_choices(lambda: PORTS)
    assert [c.path for c in choices] == [p.tty_device for p in PORTS]
    assert '067B:23A3  "USB-Serial Controller"' in choices[0].label
    assert choices[1].label == ""
    assert format_choice(choices[1]) == "/dev/tty.Bluetooth-Incoming-Port"


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="POSIX registry")
def test_a_port_one_of_our_sessions_holds_says_so():
    DaemonInfo(name="bench", pid=os.getpid(), port="/dev/cu.PL2303G-USBtoUART110",
               baud=115200, listen_host="127.0.0.1", listen_port=9600, auth="x",
               started_at=time.time()).write()
    choices = port_choices(lambda: PORTS)
    assert "held by 'bench'" in choices[0].label and "attach" in choices[0].label
    assert "held" not in choices[1].label


# ── when it asks, and when it refuses to ────────────────────────────────────


def test_a_given_port_is_used_without_asking():
    def never(scan):
        raise AssertionError("must not ask")

    assert resolve_port(_args(port="/dev/x"), choose=never) == "/dev/x"


def test_in_a_terminal_it_asks(terminal):
    asked = []

    def choose(scan):
        asked.append([c.path for c in scan()])
        return "/dev/tty.PL2303G-USBtoUART110"

    assert resolve_port(_args(), choose=choose, scan=lambda: PORTS) == \
        "/dev/tty.PL2303G-USBtoUART110"
    assert asked == [[p.tty_device for p in PORTS]]


def test_backing_out_is_said_and_returns_nothing(terminal, capsys):
    assert resolve_port(_args(), choose=lambda scan: None, scan=lambda: PORTS) is None
    assert "No port chosen" in capsys.readouterr().err


def test_without_a_terminal_it_lists_the_ports_and_fails(no_terminal, capsys):
    """A script that forgot --port must fail, not wait for a keypress."""

    def never(scan):
        raise AssertionError("must not ask without a terminal")

    assert resolve_port(_args(), choose=never, scan=lambda: PORTS) is None
    err = capsys.readouterr().err
    assert "--port is required" in err
    assert "/dev/tty.PL2303G-USBtoUART110" in err


def test_no_tui_never_asks_even_in_a_terminal(terminal, capsys):
    def never(scan):
        raise AssertionError("--no-tui must not open a picker")

    assert resolve_port(_args(no_tui=True), choose=never, scan=lambda: PORTS) is None
    assert "--port is required" in capsys.readouterr().err


def test_no_ports_at_all_is_said(no_terminal, capsys):
    assert resolve_port(_args(), scan=lambda: []) is None
    assert "No serial ports found" in capsys.readouterr().err


def test_connect_without_port_fails_cleanly_in_a_pipe(no_terminal, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_scan_ports", lambda: [])
    assert cli.main(["connect", "--no-log"]) == 1
    assert "--port is required" in capsys.readouterr().err


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="needs fork")
def test_start_without_a_port_never_asks(terminal, monkeypatch, capsys):
    """A detached daemon has nobody to ask — even from a terminal."""
    def never(*a, **k):
        raise AssertionError("start must not open a picker")

    monkeypatch.setattr("uart_proxy.ui.port_picker.pick_port", never)
    assert cli.main(["start"]) == 1
    assert "start needs --port" in capsys.readouterr().err


# ── the picker itself ───────────────────────────────────────────────────────


needs_textual = pytest.mark.skipif(not _TEXTUAL_AVAILABLE, reason="textual")


async def _settle(pilot, tries=4):
    for _ in range(tries):
        await asyncio.sleep(0.02)
        await pilot.pause()


def _pick(keys, scan):
    from uart_proxy.ui.port_picker import PortPickerApp

    async def scenario():
        app = PortPickerApp(scan)
        async with app.run_test() as pilot:
            await _settle(pilot)
            for key in keys:
                await pilot.press(key)
                await _settle(pilot)
        return app.return_value

    return asyncio.run(scenario())


CHOICES = [PortChoice("/dev/a", "first"), PortChoice("/dev/b", "second")]


@needs_textual
def test_enter_picks_the_highlighted_port():
    assert _pick(["enter"], lambda: CHOICES) == "/dev/a"


@needs_textual
def test_arrow_down_then_enter_picks_the_next():
    assert _pick(["down", "enter"], lambda: CHOICES) == "/dev/b"


@needs_textual
def test_escape_cancels():
    assert _pick(["escape"], lambda: CHOICES) is None


@needs_textual
def test_r_rescans_for_a_port_just_plugged_in():
    scans = iter([[], [PortChoice("/dev/new", "just plugged in")]])
    assert _pick(["r", "enter"], lambda: next(scans)) == "/dev/new"


@needs_textual
def test_an_empty_list_can_only_be_cancelled_or_rescanned():
    assert _pick(["enter", "escape"], lambda: []) is None


def test_the_terminal_check_needs_both_ends(monkeypatch):
    class _Stream:
        def __init__(self, tty):
            self._tty = tty

        def isatty(self):
            return self._tty

    for stdin, stdout, expected in [(True, True, True), (True, False, False),
                                    (False, True, False)]:
        monkeypatch.setattr(cli.sys, "stdin", _Stream(stdin))
        monkeypatch.setattr(cli.sys, "stdout", _Stream(stdout))
        assert cli._have_terminal() is expected
