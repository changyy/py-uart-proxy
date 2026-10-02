"""
`tail`, `expect` and `send`: a served session from a script or a shell (SPEC S41).

    uart-proxy tail usbserial-110 -n 20
    uart-proxy send usbserial-110 "uname -a" --expect Linux --timeout 5
    uart-proxy expect usbserial-110 "login:" --timeout 60 && echo booted

A session is named as in the registry (S17, S39) — the only one running needs
no name — or reached with ``--host``, ``--port`` and ``--auth``. Exit status:
0 done, 1 the expected text did not come, 2 the session could not be reached.
"""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from .client import SessionClient, SessionClientError, format_line
from .core.daemon import DaemonNotFound, prune_dead

CLIENT_NAME = "uart-proxy cli"


def _client(args: argparse.Namespace, name: Optional[str], *, full: bool) -> SessionClient:
    if args.host:
        if not args.auth:
            raise SessionClientError("--host needs --auth CODE")
        return SessionClient(args.host, args.port, args.auth, client_name=CLIENT_NAME)
    prune_dead()
    return SessionClient.from_registry(name, want_full=full, client_name=CLIENT_NAME)


def _split(words: list[str], what: str) -> tuple[Optional[str], str]:
    """``[NAME] VALUE`` — the name is optional when one session is running."""
    if len(words) == 1:
        return None, words[0]
    if len(words) == 2:
        return words[0], words[1]
    raise SessionClientError(f"expected [NAME] {what}")


def _run(fn, args: argparse.Namespace) -> int:
    try:
        return fn(args)
    except (SessionClientError, DaemonNotFound, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


def cmd_tail(args: argparse.Namespace) -> int:
    def go(args):
        with _client(args, args.name, full=False) as client:
            for line in client.tail(args.lines):
                print(format_line(line))
            partial = client.partial
            if partial:
                print(format_line(partial))
        return 0
    return _run(go, args)


def cmd_expect(args: argparse.Namespace) -> int:
    def go(args):
        name, pattern = _split(args.words, "PATTERN")
        with _client(args, name, full=False) as client:
            hit = client.expect(pattern, args.timeout)
        if hit is None:
            print(f"timed out after {args.timeout:g}s waiting for {pattern!r}", file=sys.stderr)
            return 1
        print(format_line(hit["line"]))
        return 0
    return _run(go, args)


def cmd_send(args: argparse.Namespace) -> int:
    def go(args):
        name, value = _split(args.words, "TEXT")
        with _client(args, name, full=True) as client:
            mark = client.cursor
            if args.hex:
                client.send_hex(value)
            else:
                client.send_text(value, eol=args.eol)
            if not args.expect:
                return 0
            hit = client.expect(args.expect, args.timeout, since=mark)
        if hit is None:
            print(f"sent; no {args.expect!r} within {args.timeout:g}s", file=sys.stderr)
            return 1
        print(format_line(hit["line"]))
        return 0
    return _run(go, args)


def _address_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host", default=None, help="A server's address, instead of a registry name.")
    p.add_argument("--port", type=int, default=9600, help="Its port (with --host).")
    p.add_argument("--auth", default=None, help="Its auth code (with --host).")


def add_parsers(sub) -> None:
    p = sub.add_parser("tail", help="Print a served session's last lines, stamped.")
    p.add_argument("name", nargs="?", default=None, help="Session name (see 'status').")
    p.add_argument("-n", "--lines", type=int, default=50, help="How many (default 50).")
    _address_args(p)
    p.set_defaults(func=cmd_tail)

    p = sub.add_parser("expect", help="Wait until a served session prints a pattern.")
    p.add_argument("words", nargs="+", metavar="[NAME] PATTERN",
                   help="Session name (optional), then a regular expression.")
    p.add_argument("--timeout", type=float, default=10.0, help="Seconds (default 10).")
    _address_args(p)
    p.set_defaults(func=cmd_expect)

    p = sub.add_parser("send", help="Send a line (or hex bytes) to a served session.")
    p.add_argument("words", nargs="+", metavar="[NAME] TEXT",
                   help="Session name (optional), then what to send.")
    p.add_argument("--hex", action="store_true", help="TEXT is hex bytes, e.g. 'A5 01 0d'.")
    p.add_argument("--eol", default="cr", choices=["cr", "lf", "crlf", "none"],
                   help="Line ending after TEXT (default cr).")
    p.add_argument("--expect", default=None, metavar="PATTERN",
                   help="Then wait for this regular expression in the reply.")
    p.add_argument("--timeout", type=float, default=10.0, help="Seconds to wait (default 10).")
    _address_args(p)
    p.set_defaults(func=cmd_send)
