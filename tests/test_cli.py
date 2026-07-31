"""S4/S8: CLI helpers — output dir resolution and the argument parser."""

from __future__ import annotations

import argparse
import os
from types import SimpleNamespace

import pytest

from uart_proxy.cli import _maybe_build_pty_proxy, _resolve_output_dir, build_parser
from uart_proxy.core.pty_proxy import (
    DEFAULT_PROXY_COUNT,
    DEFAULT_PROXY_DIR,
    DEFAULT_TX_MERGE,
    PTY_SUPPORTED,
)


def test_resolve_output_dir_explicit():
    args = argparse.Namespace(output_dir="/tmp/foo")
    assert _resolve_output_dir(args) == "/tmp/foo"


def test_resolve_output_dir_default_under_home():
    args = argparse.Namespace(output_dir=None)
    path = _resolve_output_dir(args)
    expected_root = os.path.expanduser("~/.uart-proxy/sessions")
    assert path.startswith(expected_root)
    # last component looks like a YYYYmmdd-HHMMSS timestamp
    assert len(os.path.basename(path)) == len("20260612-095839")


def test_parser_ports_json():
    args = build_parser().parse_args(["ports", "--json"])
    assert args.command == "ports" and args.json is True


def test_parser_connect_flags():
    args = build_parser().parse_args(
        ["connect", "--port", "/dev/ttyUSB0", "--baud", "9600",
         "--serve", "--auth", "123456", "--auth", "000000:readonly", "--no-tui"]
    )
    assert args.port == "/dev/ttyUSB0" and args.baud == 9600
    assert args.serve is True and args.auth == ["123456", "000000:readonly"]
    assert args.no_tui is True


def test_connect_default_eol_is_cr():
    # CR is the Unix-console convention; CRLF caused a double prompt.
    args = build_parser().parse_args(["connect", "--port", "/dev/ttyUSB0"])
    assert args.eol == "cr"


def test_parser_remote_flags():
    args = build_parser().parse_args(
        ["remote", "--host", "10.0.0.1", "--port", "9600", "--auth", "123456"]
    )
    assert args.command == "remote" and args.host == "10.0.0.1" and args.auth == "123456"


# ── S14/S15: PTY mirrors and the exclusive claim ────────────────────────────


def test_connect_claims_the_port_exclusively_by_default():
    args = build_parser().parse_args(["connect", "--port", "/dev/ttyUSB0"])
    assert args.no_exclusive is False


def test_parser_proxy_flags():
    args = build_parser().parse_args(
        ["connect", "--port", "/dev/cu.usbserial-110",
         "--proxy-dir", "/tmp/uart-proxy", "--proxy-count", "4",
         "--proxy", "/tmp/mine", "--tx-merge", "raw", "--no-exclusive"]
    )
    assert args.proxy_dir == "/tmp/uart-proxy" and args.proxy_count == 4
    assert args.proxy == ["/tmp/mine"] and args.tx_merge == "raw"
    assert args.no_exclusive is True


def test_proxy_defaults_are_two_raw_mirrors():
    """Raw is the default: a mirror behaves like the serial port it stands in
    for. Holding bytes to keep commands atomic is the opt-in."""
    args = build_parser().parse_args(
        ["connect", "--port", "/dev/cu.usbserial-110", "--proxy-dir"]
    )
    assert args.proxy_dir == DEFAULT_PROXY_DIR
    assert args.proxy_count == DEFAULT_PROXY_COUNT == 2
    assert args.tx_merge == DEFAULT_TX_MERGE == "raw"


def test_mirrors_are_off_unless_asked_for():
    args = build_parser().parse_args(["connect", "--port", "/dev/ttyUSB0"])
    session = SimpleNamespace(write=lambda data: len(data), publish_notice=print)
    assert _maybe_build_pty_proxy(session, args) is None


@pytest.mark.skipif(not PTY_SUPPORTED, reason="pty is POSIX-only")
def test_mirror_links_are_named_after_the_device(tmp_path):
    args = build_parser().parse_args(
        ["connect", "--port", "/dev/cu.usbserial-110",
         "--proxy-dir", str(tmp_path), "--proxy", "/tmp/explicit"]
    )
    session = SimpleNamespace(write=lambda data: len(data), publish_notice=print)
    group = _maybe_build_pty_proxy(session, args)
    assert group is not None
    assert group.links == [
        os.path.join(tmp_path, "usbserial-110-0"),
        os.path.join(tmp_path, "usbserial-110-1"),
        "/tmp/explicit",
    ]


@pytest.mark.skipif(not PTY_SUPPORTED, reason="pty is POSIX-only")
def test_proxy_alone_exposes_only_the_explicit_paths(tmp_path):
    """--proxy without --proxy-dir must not also invent numbered mirrors."""
    args = build_parser().parse_args(
        ["connect", "--port", "/dev/ttyUSB0", "--proxy", str(tmp_path / "only")]
    )
    session = SimpleNamespace(write=lambda data: len(data), publish_notice=print)
    group = _maybe_build_pty_proxy(session, args)
    assert group is not None and group.links == [str(tmp_path / "only")]
