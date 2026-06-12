"""
Command-line entry point for uart-proxy.

Subcommands
-----------
* ``ports``    list the serial ports on this machine.
* ``connect``  open a local UART for read & write (optionally also serve a proxy).
* ``remote``   attach to a remote uart-proxy server as a client.

Examples
--------
    uart-proxy ports
    uart-proxy connect --port /dev/tty.usbserial-110 --baud 115200
    uart-proxy connect --port COM3 --serve --auth 123456 --auth 000000:readonly
    uart-proxy connect --port /dev/ttyUSB0 --grep ERROR --grep "panic.*"
    uart-proxy remote --host 192.168.1.10 --port 9600 --auth 123456
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

from uart_helper import SerialMonitor, UARTConfig

from . import __version__
from .core.session import UartSession
from .core.recorder import Recorder
from .core.retention import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_MAX_TOTAL_MB,
    apply_retention,
    format_bytes,
    scan_sessions,
)
from .io.socket_source import SocketSource
from .io.uart_source import UartSource
from .plugins.manager import PluginManager
from .proxy.protocol import Role, parse_auth_spec
from .proxy.server import ProxyServer


# ── ports ─────────────────────────────────────────────────────────────────────


def cmd_ports(args: argparse.Namespace) -> int:
    monitor = SerialMonitor()
    found = monitor.scan_once()
    if args.json:
        data = [
            {
                "device": ident.tty_device,
                "raw_device": ident.device,
                "description": ident.description,
                "vid_pid": ident.vid_pid_str,
                "serial": ident.serial_number,
                "manufacturer": ident.manufacturer,
            }
            for ident, _ in found
        ]
        print(json.dumps({"status": True, "action": "ports", "data": data}))
        return 0

    if not found:
        print("No serial ports found.")
        return 0
    print(f"Found {len(found)} port(s):\n")
    for ident, _ in found:
        line = f"  {ident.tty_device}"
        if ident.vid is not None:
            line += f"  {ident.vid_pid_str}"
        if ident.description:
            line += f'  "{ident.description}"'
        if ident.serial_number:
            line += f"  serial={ident.serial_number}"
        print(line)
    return 0


# ── sessions (retention management) ────────────────────────────────────────


def cmd_sessions(args: argparse.Namespace) -> int:
    root = DEFAULT_LOG_ROOT

    if args.prune:
        max_age, max_total = _resolve_retention(args)
        report = apply_retention(
            root, max_age_days=max_age, max_total_bytes=max_total
        )
        if args.json:
            print(json.dumps({
                "status": True, "action": "prune",
                "data": {
                    "deleted": report.deleted,
                    "freed_bytes": report.freed_bytes,
                    "kept": report.kept,
                    "total_after": report.total_after,
                },
            }))
        else:
            print(f"Pruned {len(report.deleted)} session(s), "
                  f"freed {format_bytes(report.freed_bytes)}.")
            print(f"Kept {report.kept} session(s), "
                  f"{format_bytes(report.total_after)} total.")
        return 0

    sessions = scan_sessions(root)
    total = sum(s.size for s in sessions)
    if args.json:
        print(json.dumps({
            "status": True, "action": "sessions",
            "data": {
                "root": root,
                "total_bytes": total,
                "sessions": [
                    {"path": s.path, "mtime": s.mtime, "size": s.size}
                    for s in sessions
                ],
            },
        }))
        return 0

    if not sessions:
        print(f"No sessions in {root}")
        return 0
    print(f"Sessions in {root} ({format_bytes(total)} total):\n")
    now = time.time()
    for s in sorted(sessions, key=lambda x: x.mtime):
        age_days = (now - s.mtime) / 86400.0
        print(f"  {os.path.basename(s.path):20s} "
              f"{format_bytes(s.size):>10s}  {age_days:5.1f}d old")
    max_age, max_total = _resolve_retention(args)
    age_str = "disabled" if not max_age else f"{max_age:g} days"
    size_str = "disabled" if not max_total else format_bytes(max_total)
    print(f"\nPolicy: keep ≤ {age_str}, store ≤ {size_str}. "
          f"Run with --prune to apply now.")
    return 0


# ── shared wiring ───────────────────────────────────────────────────────────


def _build_config(args: argparse.Namespace) -> UARTConfig:
    return UARTConfig(
        baudrate=args.baud,
        bytesize=args.bytesize,
        parity=args.parity,
        stopbits=args.stopbits,
    )


_EOL_MAP = {"crlf": b"\r\n", "lf": b"\n", "cr": b"\r", "none": b""}

# Default recording root when --output-dir is not given.
# normpath so the path prints with native separators (e.g. on Windows
# expanduser otherwise yields a mixed "C:\\Users\\you/.uart-proxy/sessions").
DEFAULT_LOG_ROOT = os.path.normpath(os.path.expanduser("~/.uart-proxy/sessions"))
CONFIG_PATH = os.path.normpath(os.path.expanduser("~/.uart-proxy/config.toml"))


def _load_config() -> dict:
    """Read ~/.uart-proxy/config.toml if present (TOML, read-only)."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return {}
    if not os.path.isfile(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "rb") as fh:
            return tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return {}


def _resolve_retention(args: argparse.Namespace) -> tuple[float, int]:
    """Resolve (max_age_days, max_total_bytes): CLI flag > config > default.

    A value of 0 on either axis disables that dimension.
    """
    cfg = _load_config().get("retention", {})
    max_age = getattr(args, "max_age_days", None)
    if max_age is None:
        max_age = cfg.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    max_mb = getattr(args, "max_total_mb", None)
    if max_mb is None:
        max_mb = cfg.get("max_total_mb", DEFAULT_MAX_TOTAL_MB)
    return float(max_age), int(max_mb) * 1024 * 1024


def _resolve_output_dir(args: argparse.Namespace) -> str:
    """Where to write logs: --output-dir if given, else a per-session folder
    under ~/.uart-proxy/sessions/<YYYYmmdd-HHMMSS>/ so runs never clobber."""
    if args.output_dir:
        return args.output_dir
    from datetime import datetime

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(DEFAULT_LOG_ROOT, stamp)


def _attach_recorder(session: UartSession, args: argparse.Namespace) -> Optional[Recorder]:
    if args.no_log:
        return None
    recorder = Recorder(
        _resolve_output_dir(args),
        base_name=args.log_base,
        include_tx=args.log_tx,
        append=args.log_append,
    )
    session.bus.subscribe(recorder.handle)
    return recorder


def _build_plugins(session: UartSession, args: argparse.Namespace) -> PluginManager:
    manager = PluginManager(session)

    if args.grep:
        manager.add_builtin(
            "grep",
            {"patterns": list(args.grep), "ignore_case": args.grep_ignore_case},
        )

    plugin_config = {}
    if args.plugin_config:
        with open(args.plugin_config, encoding="utf-8") as fh:
            plugin_config = json.load(fh)

    for name in args.plugin or []:
        manager.add_builtin(name, plugin_config.get(name))
    for path in args.plugin_file or []:
        manager.load_file(path, plugin_config)
    for directory in args.plugin_dir or []:
        manager.load_dir(directory, plugin_config)

    return manager


def _maybe_build_proxy(session: UartSession, args: argparse.Namespace) -> Optional[ProxyServer]:
    if not args.serve:
        return None
    auth: dict[str, Role] = {}
    for spec in args.auth or []:
        code, role = parse_auth_spec(spec)
        auth[code] = role
    if not auth:
        # A sensible default so --serve alone still works (full access).
        auth["123456"] = Role.FULL
        print("No --auth given; using default code 123456 (full access).", file=sys.stderr)
    return ProxyServer(session, auth, host=args.listen, port=args.listen_port)


def _run_session(
    session: UartSession,
    args: argparse.Namespace,
    *,
    title: str,
) -> int:
    # Apply the retention policy to the managed store before opening a new
    # session folder (so the new one is never a deletion candidate).
    if args.output_dir is None and not args.no_log:
        max_age, max_total = _resolve_retention(args)
        report = apply_retention(
            DEFAULT_LOG_ROOT, max_age_days=max_age, max_total_bytes=max_total
        )
        if report.deleted:
            print(
                f"Retention: removed {len(report.deleted)} old session(s), "
                f"freed {format_bytes(report.freed_bytes)} "
                f"(store now {format_bytes(report.total_after)}).",
                file=sys.stderr,
            )

    recorder = _attach_recorder(session, args)
    plugins = _build_plugins(session, args)
    proxy = _maybe_build_proxy(session, args)

    log_dir = os.path.normpath(os.path.dirname(recorder.raw_path)) if recorder is not None else None
    if log_dir:
        print(f"Recording to {log_dir}", file=sys.stderr)

    plugins.start()
    if proxy is not None:
        proxy.start()
        print(f"Proxy listening on {args.listen}:{args.listen_port}", file=sys.stderr)

    try:
        if args.no_tui:
            from .ui.headless import run_headless

            run_headless(session, ts_mode=args.timestamp)
        else:
            from .ui.tui import run_tui

            run_tui(session, title=title, ts_mode=args.timestamp, log_hint=log_dir)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if proxy is not None:
            proxy.stop()
        plugins.stop()
        session.stop()
        if recorder is not None:
            recorder.close()
            if recorder.paths:
                print("Logs written:", file=sys.stderr)
                for path in recorder.paths:
                    print(f"  {path}", file=sys.stderr)
    return 0


# ── connect (local UART) ──────────────────────────────────────────────────────


def cmd_connect(args: argparse.Namespace) -> int:
    config = _build_config(args)
    source = UartSource(args.port, config)
    session = UartSession(
        source,
        encoding=args.encoding,
        default_eol=_EOL_MAP[args.eol],
        auto_reconnect=not args.no_reconnect,
        reconnect_interval=args.reconnect_interval,
    )
    return _run_session(session, args, title=f"uart-proxy · {args.port}")


# ── remote (socket client) ────────────────────────────────────────────────────


def cmd_remote(args: argparse.Namespace) -> int:
    source = SocketSource(args.host, args.port, args.auth)
    session = UartSession(
        source,
        encoding=args.encoding,
        default_eol=_EOL_MAP[args.eol],
        auto_reconnect=not args.no_reconnect,
        reconnect_interval=args.reconnect_interval,
    )
    # A remote client never re-serves by default; ignore --serve here.
    args.serve = False
    return _run_session(session, args, title=f"uart-proxy · {args.host}:{args.port}")


# ── argument parsing ──────────────────────────────────────────────────────────


def _add_common_io_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--encoding", default="utf-8",
                   help="Text decoding (e.g. utf-8, latin-1 for BBS). Default utf-8.")
    p.add_argument("--eol", choices=list(_EOL_MAP), default="cr",
                   help="Line ending appended to sent text. Default cr — the "
                        "convention for Unix consoles (PuTTY/minicom/screen); "
                        "crlf can cause a double newline / double prompt.")
    p.add_argument("--timestamp", choices=["none", "relative", "full"], default="relative",
                   help="Initial timestamp display mode. Default relative.")
    p.add_argument("--no-tui", action="store_true",
                   help="Headless stream to stdout instead of the TUI.")
    # reconnect
    p.add_argument("--no-reconnect", action="store_true",
                   help="Don't wait for / auto-reattach the device; fail if absent.")
    p.add_argument("--reconnect-interval", type=float, default=1.0,
                   help="Seconds between reconnect attempts. Default 1.0.")
    # logging
    p.add_argument("--output-dir", default=None,
                   help="Directory for log files. Default: "
                        "~/.uart-proxy/sessions/<timestamp>/ (use '.' for cwd).")
    p.add_argument("--log-base", default="output", help="Log file base name.")
    p.add_argument("--no-log", action="store_true", help="Disable file recording.")
    p.add_argument("--log-tx", action="store_true",
                   help="Also record TX lines in the timestamped logs.")
    p.add_argument("--log-append", action="store_true",
                   help="Append to existing log files instead of overwriting.")
    # retention (only applies to the default ~/.uart-proxy/sessions store)
    p.add_argument("--max-age-days", type=float, default=None,
                   help=f"Delete sessions older than N days "
                        f"(default {DEFAULT_MAX_AGE_DAYS}; 0=disable).")
    p.add_argument("--max-total-mb", type=int, default=None,
                   help=f"Cap total size of the session store in MB; delete "
                        f"oldest when exceeded (default {DEFAULT_MAX_TOTAL_MB}; 0=disable).")
    # plugins
    p.add_argument("--grep", action="append", metavar="PATTERN",
                   help="Highlight lines matching PATTERN (repeatable).")
    p.add_argument("--grep-ignore-case", action="store_true",
                   help="Case-insensitive --grep matching.")
    p.add_argument("--plugin", action="append", metavar="NAME",
                   help="Load a built-in plugin by name (repeatable).")
    p.add_argument("--plugin-file", action="append", metavar="PATH",
                   help="Load a user plugin .py file (repeatable).")
    p.add_argument("--plugin-dir", action="append", metavar="DIR",
                   help="Load all plugins in a directory (repeatable).")
    p.add_argument("--plugin-config", metavar="JSON",
                   help="JSON file mapping plugin name -> config dict.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uart-proxy",
        description="UART log reader / controller with timestamping, recording, "
                    "socket proxy, and a plugin system.",
    )
    parser.add_argument("--version", action="version", version=f"uart-proxy {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # ports
    p_ports = sub.add_parser("ports", help="List serial ports on this machine.")
    p_ports.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_ports.set_defaults(func=cmd_ports)

    # sessions
    p_sess = sub.add_parser("sessions",
                            help="List or prune recorded sessions (~/.uart-proxy/sessions).")
    p_sess.add_argument("--prune", action="store_true",
                        help="Apply the retention policy now (delete old/oversized).")
    p_sess.add_argument("--max-age-days", type=float, default=None,
                        help=f"Override max age in days (default {DEFAULT_MAX_AGE_DAYS}; 0=disable).")
    p_sess.add_argument("--max-total-mb", type=int, default=None,
                        help=f"Override store size cap in MB (default {DEFAULT_MAX_TOTAL_MB}; 0=disable).")
    p_sess.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_sess.set_defaults(func=cmd_sessions)

    # connect
    p_conn = sub.add_parser("connect", help="Open a local UART for read & write.")
    p_conn.add_argument("--port", required=True, help="Serial device path or COM port.")
    p_conn.add_argument("--baud", type=int, default=115200, help="Baud rate. Default 115200.")
    p_conn.add_argument("--bytesize", type=int, default=8, choices=[5, 6, 7, 8])
    p_conn.add_argument("--parity", default="N", choices=["N", "E", "O", "M", "S"])
    p_conn.add_argument("--stopbits", type=float, default=1, choices=[1, 1.5, 2])
    p_conn.add_argument("--serve", action="store_true",
                        help="Also expose this session via a socket proxy.")
    p_conn.add_argument("--listen", default="0.0.0.0", help="Proxy bind address.")
    p_conn.add_argument("--listen-port", type=int, default=9600, help="Proxy port.")
    p_conn.add_argument("--auth", action="append", metavar="CODE[:role]",
                        help="Auth code, optional role (full|readonly). Repeatable.")
    _add_common_io_args(p_conn)
    p_conn.set_defaults(func=cmd_connect)

    # remote
    p_rem = sub.add_parser("remote", help="Attach to a remote uart-proxy server.")
    p_rem.add_argument("--host", required=True, help="Remote server address.")
    p_rem.add_argument("--port", type=int, default=9600, help="Remote server port.")
    p_rem.add_argument("--auth", required=True, help="Auth code for the remote server.")
    _add_common_io_args(p_rem)
    p_rem.set_defaults(func=cmd_remote)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
