"""S30: `--profile` — a uart_helper device profile sets the port and settings."""

from __future__ import annotations

import argparse
import sys

import pytest
from uart_helper import PortIdentity

from uart_proxy import cli
from uart_proxy.cli import _build_config, apply_profile, resolve_port
from uart_proxy.core import daemon as daemon_mod

pytestmark = pytest.mark.skipif(sys.version_info < (3, 11) and
                                __import__("importlib").util.find_spec("tomli") is None,
                                reason="profiles are TOML")

PROFILE = """
description = "Lab boards"

[defaults]
baudrate = 9600
parity = "E"
stopbits = 2
rtscts = true

[[rules]]
vid = "067b"
pid = "23a3"
label = "PL2303"
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv(daemon_mod.HOME_ENV, str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)          # no ./uart-helper.d/ of the developer's
    monkeypatch.setattr(cli, "CONFIG_PATH", str(tmp_path / "absent.toml"))


@pytest.fixture
def profile(tmp_path):
    directory = tmp_path / "xdg" / "uart-helper"
    directory.mkdir(parents=True)
    path = directory / "lab.toml"
    path.write_text(PROFILE)
    return path


def _args(argv):
    return cli.build_parser().parse_args(argv)


PL2303 = PortIdentity(device="/dev/cu.PL2303G-USBtoUART110", vid=0x067B, pid=0x23A3)
OTHER = PortIdentity(device="/dev/cu.usbserial-9", vid=0x0403, pid=0x6001)


# ── settings ────────────────────────────────────────────────────────────────


def test_without_a_profile_the_built_in_defaults_apply():
    args = _args(["connect", "--port", "/dev/x"])
    assert apply_profile(args) is None
    assert (args.baud, args.bytesize, args.parity, args.stopbits) == (115200, 8, "N", 1.0)
    config = _build_config(args)
    assert config.rtscts is False and config.baudrate == 115200


def test_a_profile_by_name_sets_what_no_flag_does(profile):
    args = _args(["connect", "--port", "/dev/x", "--profile", "lab"])
    assert apply_profile(args) is None
    assert (args.baud, args.parity, args.stopbits) == (9600, "E", 2.0)
    assert args.bytesize == 8, "absent from the profile: the built-in default"
    assert _build_config(args).rtscts is True, "flow control comes through too"


def test_a_flag_beats_the_profile_even_when_it_equals_the_default(profile):
    """--baud 115200 is a choice, not an absence."""
    args = _args(["connect", "--port", "/dev/x", "--profile", "lab", "--baud", "115200"])
    apply_profile(args)
    assert args.baud == 115200 and args.parity == "E"


def test_a_profile_by_path(profile):
    args = _args(["connect", "--port", "/dev/x", "--profile", str(profile)])
    assert apply_profile(args) is None and args.baud == 9600


def test_an_unknown_profile_is_an_error_that_says_where_it_looked(capsys):
    assert cli.main(["connect", "--port", "/dev/x", "--profile", "nope"]) == 1
    err = capsys.readouterr().err
    assert "Profile 'nope' not found" in err


def test_a_broken_profile_is_an_error_not_a_crash(tmp_path, capsys):
    bad = tmp_path / "bad.toml"
    bad.write_text("this is = = not toml")
    assert cli.main(["connect", "--port", "/dev/x", "--profile", str(bad)]) == 1
    assert "Failed to parse" in capsys.readouterr().err


def test_settling_twice_changes_nothing(profile):
    """`start` settles before detaching; the `connect` it runs must not redo it."""
    args = _args(["start", "--port", "/dev/x", "--profile", "lab"])
    apply_profile(args)
    args.profile = "nope"          # would fail if it were loaded again
    assert apply_profile(args) is None and args.baud == 9600


# ── the port ────────────────────────────────────────────────────────────────


def _resolved(argv, ports, **kw):
    args = _args(argv)
    assert apply_profile(args) is None
    return resolve_port(args, scan=lambda: ports, **kw)


def test_the_one_matching_port_is_taken(profile, capsys):
    assert _resolved(["connect", "--profile", "lab"], [OTHER, PL2303]) == \
        PL2303.tty_device
    assert "matched" in capsys.readouterr().err


def test_no_matching_port_is_said(profile, capsys):
    assert _resolved(["connect", "--profile", "lab"], [OTHER]) is None
    assert "no connected port matches profile 'lab'" in capsys.readouterr().err


def test_several_matches_are_offered_and_only_those(profile, monkeypatch):
    monkeypatch.setattr(cli, "_have_terminal", lambda: True)
    twin = PortIdentity(device="/dev/cu.PL2303G-USBtoUART120", vid=0x067B, pid=0x23A3)
    offered = []

    def choose(scan):
        offered.append([c.path for c in scan()])
        return offered[0][1]

    args = _args(["connect", "--profile", "lab"])
    apply_profile(args)
    chosen = resolve_port(args, choose=choose, scan=lambda: [PL2303, OTHER, twin])
    assert offered == [[PL2303.tty_device, twin.tty_device]]
    assert chosen == twin.tty_device


def test_several_matches_without_a_terminal_fail_and_list_them(profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_have_terminal", lambda: False)
    twin = PortIdentity(device="/dev/cu.PL2303G-USBtoUART120", vid=0x067B, pid=0x23A3)
    assert _resolved(["connect", "--profile", "lab"], [PL2303, twin]) is None
    err = capsys.readouterr().err
    assert "several ports match the profile" in err
    assert PL2303.tty_device in err and twin.tty_device in err
    assert OTHER.tty_device not in err


def test_an_explicit_port_wins_over_the_rules(profile):
    assert _resolved(["connect", "--profile", "lab", "--port", "/dev/mine"], [PL2303]) \
        == "/dev/mine"


def test_a_profile_without_rules_leaves_the_port_to_you(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_have_terminal", lambda: False)
    rules_free = tmp_path / "plain.toml"
    rules_free.write_text("[defaults]\nbaudrate = 57600\n")
    assert _resolved(["connect", "--profile", str(rules_free)], [PL2303]) is None
    assert "--port is required" in capsys.readouterr().err


# ── start ───────────────────────────────────────────────────────────────────


def test_start_with_neither_port_nor_profile_says_what_it_needs(capsys):
    assert cli.main(["start"]) == 1
    assert "start needs --port, or a --profile" in capsys.readouterr().err


def test_start_with_a_profile_that_matches_nothing_fails_before_detaching(
        profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_scan_ports", lambda: [OTHER])
    monkeypatch.setattr(cli, "daemonize", lambda **kw: pytest.fail("must not detach"))
    assert cli.main(["start", "--profile", "lab"]) == 1
    assert "no connected port matches" in capsys.readouterr().err


def test_start_with_several_matches_refuses_rather_than_asks(profile, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_have_terminal", lambda: True)
    twin = PortIdentity(device="/dev/cu.PL2303G-USBtoUART120", vid=0x067B, pid=0x23A3)
    monkeypatch.setattr(cli, "_scan_ports", lambda: [PL2303, twin])
    monkeypatch.setattr("uart_proxy.ui.port_picker.pick_port",
                        lambda *a: pytest.fail("start must not ask"))
    assert cli.main(["start", "--profile", "lab"]) == 1
    assert "several ports match" in capsys.readouterr().err


def test_the_suite_never_scans_real_ports_by_default():
    """The guard in conftest.py: this is what keeps an attached adapter safe."""
    assert cli._scan_ports() == []
    with pytest.raises(AssertionError, match="tried to detach"):
        cli.daemonize()
