"""S4/S8: CLI helpers — output dir resolution and the argument parser."""

from __future__ import annotations

import argparse
import os

from uart_proxy.cli import _resolve_output_dir, build_parser


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
