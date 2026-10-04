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
import signal
import sys
import time
from typing import Callable, Optional

from uart_helper import SerialMonitor, UARTConfig

from . import __version__
from .core.daemon import (
    DAEMON_SUPPORTED,
    DaemonInfo,
    DaemonNotFound,
    best_code,
    codes_by_role,
    connect_host,
    daemonize,
    find_daemon,
    list_daemons,
    new_auth_code,
    prune_dead,
    read_state,
    register_served,
    stop_daemon,
    unique_name,
)
from .core.events import EventKind
from .core.port_busy import describe_busy, same_device
from .core.port_identity import clean_description, describe as describe_port, sort_ports
from .core.pty_proxy import (
    DEFAULT_MAX_LAG,
    DEFAULT_PROXY_COUNT,
    DEFAULT_PROXY_DIR,
    DEFAULT_TX_MERGE,
    PTY_SUPPORTED,
    TX_MERGE_MODES,
    PtyProxyGroup,
    build_links,
    device_stem,
)
from .core.replay import DEFAULT_REPLAY_LINES, ReplayBuffer, describe
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
from .io.url_source import SCHEMES as URL_SCHEMES
from .io.ssh_source import SshSource, parse_size
from .io.telnet_source import TelnetSource
from .io.url_source import UrlSource, check_port_url, is_port_url
from .ui.keymap import DEFAULT_PREFIX, normalise_prefix, prefix_label
from .io.uart_source import UartSource
from .plugins.manager import PluginManager
from .core.triggers import Triggers
from .proxy.protocol import Role, parse_auth_spec
from .proxy.server import MAX_CLIENTS, ProxyServer, TriggerPolicy


# ── ports ─────────────────────────────────────────────────────────────────────


def _scan_ports() -> list:
    """The ports on this machine, as ``PortIdentity`` objects."""
    return [ident for ident, _ in SerialMonitor().scan_once()]


def port_choices(scan: Optional[Callable[[], list]] = None) -> list:
    """The ports, described for the picker, with any our sessions hold marked."""
    from .ui.port_picker import PortChoice

    scan = scan or _scan_ports  # looked up now, so it can be replaced

    try:
        held = list_daemons()
    except OSError:
        held = []
    choices = []
    for ident in sort_ports(scan()):
        parts = [describe_port(ident)] if describe_port(ident) else []
        holder = next((d for d in held if same_device(d.port, ident.tty_device)), None)
        if holder is not None:
            parts.append(f"(held by '{holder.name}' — attach instead)")
        choices.append(PortChoice(ident.tty_device, "  ".join(parts)))
    return choices


def _have_terminal() -> bool:
    """Whether someone is at a terminal to answer a question."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def resolve_port(args: argparse.Namespace,
                 choose: Optional[Callable[[Callable[[], list]], Optional[str]]] = None,
                 scan: Optional[Callable[[], list]] = None,
                 *, allow_picker: bool = True) -> Optional[str]:
    """``--port``, or one chosen from a list when it was left out (SPEC S27).

    Only where someone can answer: without a terminal, or with ``--no-tui``,
    the ports are listed and the command fails — a script that forgot
    ``--port`` must not sit waiting for a keypress.
    """
    if args.port:
        return args.port
    # Looked up at call time, not bound as a default: a default is fixed when
    # the function is defined, so replacing _scan_ports would not reach it.
    scan = scan or _scan_ports
    rules = getattr(args, "profile_rules", None) or []
    if rules:
        # The profile says which device it is for: take the one that matches.
        full_scan = scan
        scan = lambda: [p for p in full_scan() if any(r.matches(p) for r in rules)]  # noqa: E731
        matched = scan()
        name = getattr(args, "profile_name", "the profile")
        if len(matched) == 1:
            print(f"Profile '{name}' matched {matched[0].tty_device}", file=sys.stderr)
            return matched[0].tty_device
        if not matched:
            print(f"Error: no connected port matches profile '{name}' "
                  f"({len(rules)} rule(s)); plug it in, or pass --port.",
                  file=sys.stderr)
            return None
    if not allow_picker or args.no_tui or not _have_terminal():
        from .ui.port_picker import format_choice

        choices = port_choices(scan)
        why = ("several ports match the profile" if rules
               else "no terminal to choose in")
        print(f"Error: --port is required here ({why}).", file=sys.stderr)
        if choices:
            print("Ports on this machine:", file=sys.stderr)
            for choice in choices:
                print(f"  {format_choice(choice)}", file=sys.stderr)
        else:
            print("No serial ports found.", file=sys.stderr)
        return None
    if choose is None:
        from .ui.port_picker import pick_port as choose
    chosen = choose(lambda: port_choices(scan))
    if chosen is None:
        print("No port chosen.", file=sys.stderr)
    return chosen


def cmd_ports(args: argparse.Namespace) -> int:
    # USB adapters first; ports with no USB identity (built-in UARTs, macOS's
    # virtual nodes) after them — listed, since some of them are real UARTs.
    found = [(ident, None) for ident in sort_ports(_scan_ports())]
    if args.json:
        data = [
            {
                "device": ident.tty_device,
                "raw_device": ident.device,
                "description": clean_description(ident.description),
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
        details = describe_port(ident)
        print(f"  {ident.tty_device}  {details}".rstrip())
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


#: Serial settings a flag, a profile, or these fill in — in that order.
SERIAL_DEFAULTS = {"baud": 115200, "bytesize": 8, "parity": "N", "stopbits": 1.0}
_PROFILE_FIELDS = {"baud": "baudrate", "bytesize": "bytesize",
                   "parity": "parity", "stopbits": "stopbits"}


def _load_profile(spec: str):
    """A ``uart_helper`` profile by name, or by path to its ``.toml``."""
    from uart_helper.config import load_profile, load_profile_by_name

    if spec.endswith(".toml") or os.sep in spec or "/" in spec:
        return load_profile(os.path.expanduser(spec))
    return load_profile_by_name(spec)


def apply_profile(args: argparse.Namespace) -> Optional[str]:
    """Settle the serial settings, from ``--profile`` where flags are absent.

    SPEC S30. The flags default to None so that "not given" can be told from
    "given as the default": ``--profile lab --baud 9600`` must mean 9600 even
    though 115200 is what the profile — and the built-in default — would say.
    Returns an error message, or None. Idempotent: ``start`` settles these
    before detaching, and the ``connect`` it runs must not redo it.
    """
    if getattr(args, "_serial_settled", False):
        return None
    profile = None
    spec = getattr(args, "profile", None)
    if spec:
        try:
            profile = _load_profile(spec)
        except (FileNotFoundError, ValueError, ImportError) as exc:
            return str(exc)
    for flag, field_name in _PROFILE_FIELDS.items():
        if getattr(args, flag, None) is None:
            value = (getattr(profile.defaults, field_name) if profile is not None
                     else SERIAL_DEFAULTS[flag])
            setattr(args, flag, value)
    defaults = profile.defaults if profile is not None else UARTConfig()
    args.flow = {"xonxoff": defaults.xonxoff, "rtscts": defaults.rtscts,
                 "dsrdtr": defaults.dsrdtr}
    args.profile_rules = list(profile.rules) if profile is not None else []
    args.profile_name = profile.name if profile is not None else None
    args._serial_settled = True
    return None


def _build_config(args: argparse.Namespace) -> UARTConfig:
    flow = getattr(args, "flow", {})
    return UARTConfig(
        baudrate=args.baud,
        bytesize=args.bytesize,
        parity=args.parity,
        stopbits=args.stopbits,
        **flow,
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


def config_auth_specs() -> list[str]:
    """Fixed proxy codes from ``[proxy] auth`` in config.toml (SPEC S24).

    For a code that survives restarts — so people connecting from elsewhere
    are not sent a new one each time — without putting it on a command line,
    where ``ps`` shows it to every local user and the shell history keeps it.
    Refused, with a note, when the file is readable by others: a code in a
    world-readable file is not a secret. Anything unusable yields [], which
    means a generated code: failing safe.
    """
    if not os.path.isfile(CONFIG_PATH):
        return []
    try:
        import tomllib  # noqa: F401
    except ImportError:  # Python 3.10
        print(f"Note: {CONFIG_PATH} needs Python 3.11+ (tomllib) to be read; "
              f"ignoring it", file=sys.stderr)
        return []
    specs = _load_config().get("proxy", {}).get("auth")
    if specs is None:
        return []
    if isinstance(specs, str):
        specs = [specs]
    if not isinstance(specs, list) or not all(isinstance(x, str) and x for x in specs):
        print(f"Note: [proxy] auth in {CONFIG_PATH} must be a list of "
              f"\"CODE[:role]\" strings; ignoring it", file=sys.stderr)
        return []
    if os.name == "posix":
        mode = os.stat(CONFIG_PATH).st_mode & 0o777
        if mode & 0o077:
            print(f"Note: ignoring [proxy] auth — {CONFIG_PATH} is readable by "
                  f"others (mode {mode:o}); 'chmod 600' it to use it",
                  file=sys.stderr)
            return []
    return specs


def resolve_auth_specs(args: argparse.Namespace) -> list[str]:
    """``--auth`` if given, else config.toml's ``[proxy] auth``, else []."""
    return list(args.auth or []) or config_auth_specs()


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
        rotate_bytes=int(getattr(args, "log_rotate_mb", 0) * 1024 * 1024),
        keep_parts=getattr(args, "log_keep_parts", 0),
    )
    recorder.set_banner(session_header(session, args))
    session.bus.subscribe(recorder.handle)
    return recorder


def _utc_offset() -> str:
    offset = time.strftime("%z")          # "+0800"
    return f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset


def session_header(session: UartSession, args: argparse.Namespace) -> list[str]:
    """The comment lines a recording opens with (SPEC S25).

    Enough to read the file cold, months later: what produced it, from which
    port with which settings, and when elapsed 0 was on the wall clock — the
    elapsed column is meaningless without that.
    """
    start = session.tracker.start_wall.strftime("%Y-%m-%d %H:%M:%S")
    eol = next((k for k, v in _EOL_MAP.items() if v == session.default_eol), "?")
    return [
        f"uart-proxy {__version__} · {session.source.description()}",
        f"encoding {session.encoding} · eol {eol} · "
        f"started {start} {_utc_offset()} (elapsed 0 = this instant)",
    ]


def session_footer(session: UartSession) -> str:
    """The closing line: the whole window, in both time axes."""
    end = session.tracker.stamp()
    return (f"ended · {session.tracker.abs_window(end)} · "
            f"{session.tracker.rel_window(end)}")


def _build_triggers(session: UartSession, args: argparse.Namespace,
                    recorder: Optional[Recorder]) -> Optional[Triggers]:
    """S46: rules from --rules files; their events beside the recording."""
    files = getattr(args, "rules", None) or []
    if not files and not getattr(args, "serve", False):
        return None
    events_path = f"{recorder.raw_path[:-len('.log')]}-events.jsonl" if recorder is not None else None
    triggers = Triggers(session, events_path=events_path)
    approve = getattr(args, "approve_rules", False)
    for path in files:
        triggers.load(path, approve=approve)
    if not files:
        return triggers                      # serving: for the clients' watches (S47)
    held = [r["name"] for r in triggers.list() if r["level"] and not r["enabled"]]
    count = len(triggers.list())
    print(f"Rules: {count} loaded" + (f"; {len(held)} that send are off — --approve-rules to let them act"
                                      if held else ""), file=sys.stderr)
    return triggers


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


def _maybe_build_proxy(session: UartSession, args: argparse.Namespace,
                       triggers: Optional[Triggers] = None) -> Optional[ProxyServer]:
    if not args.serve:
        return None
    auth: dict[str, Role] = {}
    specs = resolve_auth_specs(args)
    for spec in specs:
        code, role = parse_auth_spec(spec)
        auth[code] = role
    if auth and not args.auth:
        print(f"Using {len(auth)} auth code(s) from {CONFIG_PATH}.", file=sys.stderr)
    if not auth:
        # --serve alone still works, but never with a code anyone could guess:
        # it used to be a fixed 123456 with full access, on every interface.
        code = new_auth_code()
        auth[code] = Role.FULL
        print(f"No --auth given; generated code {code} (full access).",
              file=sys.stderr)
    # History for attaching clients (S18). Subscribed here rather than inside the
    # server so it fills from the moment the session starts, not from whenever a
    # client happens to connect.
    replay = ReplayBuffer(getattr(args, "replay_lines", DEFAULT_REPLAY_LINES))
    if replay.enabled:
        session.bus.subscribe(replay.handle)
    return ProxyServer(session, auth, host=args.listen, port=args.listen_port,
                       replay=replay if replay.enabled else None,
                       max_clients=getattr(args, "max_clients", MAX_CLIENTS),
                       echo_tx=getattr(args, "echo_tx", False),
                       # S47: clients may watch, within --max-watches; nobody here
                       # can be asked about a proposal, so none is accepted.
                       triggers=triggers,
                       trigger_policy=TriggerPolicy(max_watches=getattr(args, "max_watches", 3)))


def attach_exclusivity_report(
    session: UartSession, source: UartSource, *, requested: bool
) -> None:
    """Say, once connected, whether the port really was claimed exclusively.

    The claim happens inside ``source.open()`` on the session's own connection
    thread, so the result is not known when the CLI prints its banner. And the
    claim is best-effort by design (SPEC S15) — a silent failure would leave the
    operator believing the wire is protected when it isn't, so it gets said out
    loud, as a NOTICE that both the TUI and the headless stream already render.

    Re-reports only when the answer *changes*, so a flapping device reconnecting
    every second doesn't fill the log with the same line.
    """
    last: list[bool] = []

    def on_event(event) -> None:
        if event.kind is not EventKind.STATUS or event.text != "connected":
            return
        claimed = bool(getattr(source, "is_exclusive", False))
        if last and last[-1] == claimed:
            return
        last.append(claimed)
        path = getattr(source, "device_path", "the port")
        if claimed:
            session.publish_notice(
                f"exclusive: claimed {path} (TIOCEXCL) — "
                f"another program opening it now gets EBUSY"
            )
        elif requested:
            session.publish_notice(
                f"exclusive: COULD NOT claim {path} — another program can open "
                f"it and silently split the byte stream with us"
            )
        else:
            session.publish_notice(
                f"exclusive: not claimed (--no-exclusive) — another program can "
                f"open {path} and silently split the byte stream with us"
            )

    session.bus.subscribe(on_event)


def attach_busy_report(
    session: UartSession,
    source: UartSource,
    describe: Callable[[str], str] = describe_busy,
) -> None:
    """Say who holds the port when opening it fails because it is in use.

    The session retries a failed open every ``--reconnect-interval`` and reports
    each attempt as ``waiting`` with the raw error — ``Resource busy`` — which is
    true and useless. So the first busy attempt of a streak gets a NOTICE naming
    the holder and what to do instead (SPEC S21): usually ``attach``, because
    the usual holder is a background session of ours. Once per streak, because
    ``describe`` may run ``lsof`` and the answer does not change every second;
    a successful open ends the streak, so a later one is reported afresh.
    """
    reported = [False]

    def on_event(event) -> None:
        if event.kind is not EventKind.STATUS:
            return
        if event.text == "connected":
            reported[0] = False
        elif event.text == "waiting" and getattr(source, "busy", False):
            if not reported[0]:
                reported[0] = True
                path = getattr(source, "device_path", "the port")
                session.publish_notice(describe(path))

    session.bus.subscribe(on_event)


def _term_size(args: argparse.Namespace) -> tuple[bool, Optional[tuple[int, int]]]:
    """``--term-size``: (ok, size), size None for ``auto`` (follow the view)."""
    text = getattr(args, "term_size", None)
    if not text or text.lower() == "auto":
        return True, None
    try:
        return True, parse_size(text)
    except ValueError as exc:
        print(f"Error: --term-size: {exc}", file=sys.stderr)
        return False, None


def _telnet_source(args: argparse.Namespace) -> Optional[TelnetSource]:
    """A ``telnet://`` port (SPEC S36), or None after saying what is wrong."""
    ok, size = _term_size(args)
    if not ok:
        return None
    try:
        return TelnetSource(args.port, size=size)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def _ssh_source(args: argparse.Namespace) -> Optional[SshSource]:
    """An ``ssh://`` port (SPEC S35), or None after saying what is wrong."""
    from .io.ssh_source import SSH_SUPPORTED

    if not SSH_SUPPORTED:
        print("Error: ssh:// needs a POSIX pty (macOS or Linux).", file=sys.stderr)
        return None
    ok, size = _term_size(args)
    if not ok:
        return None
    extra = []
    for option in getattr(args, "ssh_option", None) or []:
        extra += ["-o", option]
    try:
        return SshSource(args.port, command=getattr(args, "ssh_command", None),
                         size=size, extra_args=extra)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return None


def attach_network_note(session: UartSession, source) -> None:
    """Say once, on connecting, what a network port does and doesn't give.

    Where a local port reports its exclusive claim (S15), a network one has
    none to report — and whether our baud reached the far port depends on the
    scheme — so say which, rather than leave the local story implied.
    """
    said = [False]

    def on_event(event) -> None:
        if said[0] or event.kind is not EventKind.STATUS or event.text != "connected":
            return
        said[0] = True
        scheme = source.device_path.split("://", 1)[0]
        session.publish_notice(
            f"network port ({URL_SCHEMES.get(scheme, scheme)}): no exclusive "
            f"claim to take — the server decides who else may connect")

    session.bus.subscribe(on_event)


def attach_move_report(session: UartSession, source: UartSource) -> None:
    """Say so when the adapter came back under another name (SPEC S31).

    And keep the registry honest: a session of ours (background, or a
    foreground ``--serve``) is recorded under the port it holds, which
    ``status``, the busy hint and ``start``'s refusal all go by.
    """
    def on_moved(old: str, new: str) -> None:
        who = describe_port(source.identity) if source.identity is not None else ""
        session.publish_notice(
            f"device moved: {old} → {new} — the same adapter ({who}), "
            f"re-enumerated after a replug; following it")
        try:
            for info in list_daemons():
                if info.pid == os.getpid():
                    info.port = new
                    info.write()
        except OSError as exc:
            session.publish_notice(f"could not update the session registry: {exc}")

    source.on_moved = on_moved


def _maybe_build_pty_proxy(
    session: UartSession, args: argparse.Namespace
) -> Optional[PtyProxyGroup]:
    """Build the local PTY mirror group, if the user asked for one.

    Enabled by ``--proxy-dir`` or any ``--proxy PATH`` — mirroring the way
    ``--serve`` enables the socket proxy.
    """
    proxy_dir = getattr(args, "proxy_dir", None)
    explicit = list(getattr(args, "proxy", None) or [])
    if not proxy_dir and not explicit:
        return None
    if not PTY_SUPPORTED:
        raise RuntimeError(
            "--proxy-dir/--proxy need a POSIX platform (Windows has no pty); "
            "use --serve for a TCP proxy instead"
        )

    count = getattr(args, "proxy_count", DEFAULT_PROXY_COUNT)
    # A named session names its mirrors too: `--name router` should give you
    # router-0/router-1, one handle for the whole thing. Unnamed (plain
    # `connect`) falls back to the device stem, as before.
    stem = (getattr(args, "mirror_stem", None) or getattr(args, "name", None)
            or device_stem(str(getattr(args, "port", "") or "uart")))
    links = build_links(
        proxy_dir or DEFAULT_PROXY_DIR,
        stem,
        count if proxy_dir else 0,
        extra=explicit,
    )
    return PtyProxyGroup(
        links,
        session.write,
        tx_merge=args.tx_merge,
        max_lag=getattr(args, "proxy_max_lag", DEFAULT_MAX_LAG),
        on_notice=session.publish_notice,
    )


def _report_mirrors(group: PtyProxyGroup, port: str) -> None:
    stats = group.stats()
    print(
        f"Sharing {port} via {len(stats)} PTY mirror(s), tx-merge={group.tx_merge}:",
        file=sys.stderr,
    )
    for mirror in stats:
        print(f"  {mirror.link} -> {mirror.slave}", file=sys.stderr)
    if stats:
        print(f"Attach with:  screen {stats[0].link}", file=sys.stderr)


def close_recorder(recorder: Recorder) -> list[str]:
    """Close the recorder and return the files it wrote.

    ``Recorder.paths`` is derived from the *open* file handles, and ``close()``
    drops them — so the paths have to be read first. Reading them afterwards
    yields an empty list, which is how the "Logs written:" summary managed to be
    dead code from the start.
    """
    paths = [path for part in recorder.parts for path in part] + list(recorder.paths)
    recorder.close()
    return paths


def _trap_sigterm():
    """Turn SIGTERM into KeyboardInterrupt so ``finally`` cleanup still runs.

    Installed for *every* session, not just the ones holding an obvious resource:
    without it `kill` bypasses the whole shutdown path, and which resources that
    strands then depends on remembering to come back here each time one is added.
    `kill -9` is still unstoppable by definition — see SPEC S16.

    Returns a callable that restores the previous handler, or None if this
    platform/thread can't install one.
    """
    def _raise(signum, frame):  # noqa: ANN001, ARG001
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGTERM, _raise)
    except (ValueError, AttributeError, OSError):
        return None  # not the main thread, or no SIGTERM here

    def _restore() -> None:
        try:
            signal.signal(signal.SIGTERM, previous)
        except (ValueError, OSError):
            pass

    return _restore


def _codes_by_role(proxy: ProxyServer) -> dict[str, str]:
    return codes_by_role(proxy.auth)


def _best_code(codes: dict[str, str]) -> str:
    """The code ``attach`` should use: full access if there is one."""
    return best_code(codes)


def register_foreground(args: argparse.Namespace, proxy: ProxyServer,
                        log_dir: Optional[str]) -> Optional[DaemonInfo]:
    """Put a ``connect --serve`` in the session registry (SPEC S23, S39).

    The generated auth code scrolls away with the terminal; the registry is
    where ``status --show-auth`` and ``attach`` already look for a background
    session's, so a foreground one goes there too — same private file, removed
    on exit, pruned by the next command if we die without tidying up. Every OS:
    the registry's liveness probe asks Windows properly (S39).
    """
    return register_served(
        proxy, name=device_stem(args.port), port=args.port, baud=args.baud,
        log_dir=log_dir, proxy_dir=getattr(args, "proxy_dir", None), version=__version__,
    )


def session_info_lines(
    proxy: Optional[ProxyServer],
    *,
    listen: str,
    registered: Optional[DaemonInfo] = None,
    mirrors: Optional[PtyProxyGroup] = None,
    log_dir: Optional[str] = None,
) -> list[str]:
    """What `<prefix> i` shows: how to reach this session, codes included.

    Shown on this screen only, never published on the bus — a NOTICE is
    forwarded to every proxy client, read-only ones too, and would hand them the
    full-access code.
    """
    lines: list[str] = []
    if proxy is not None:
        lines.append(f"proxy    {listen}:{proxy.port}")
        for code, role in _codes_by_role(proxy).items():
            lines.append(f"auth     {code}  ({role})")
    else:
        lines.append("proxy    off (--serve to share this session)")
    if registered is not None:
        lines.append(f"attach   uart-proxy attach {registered.name}")
    if mirrors is not None:
        for mirror in mirrors.stats():
            lines.append(f"mirror   {mirror.link}")
    if log_dir:
        lines.append(f"logs     {log_dir}")
    return lines


def _run_session(
    session: UartSession,
    args: argparse.Namespace,
    *,
    title: str,
    on_ready: Optional[Callable[[], None]] = None,
    history: Optional[Callable[[], list]] = None,
    detachable: bool = False,
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

    try:
        prefix = normalise_prefix(getattr(args, "prefix", None) or DEFAULT_PREFIX)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # A plain SIGTERM (not just Ctrl-C) has to reach the cleanup in `finally`
    # rather than killing us outright — mirror symlinks are the most visible
    # thing it would strand, but proxy clients and open log files matter too.
    # Trapped before anything is built, and everything built after it is inside
    # the `try`: a signal during start-up (registering, binding) still unwinds.
    restore_term = _trap_sigterm()
    recorder = plugins = triggers = proxy = mirrors = registered = None
    try:
        recorder = _attach_recorder(session, args)
        plugins = _build_plugins(session, args)
        try:
            triggers = _build_triggers(session, args, recorder)
        except (OSError, ValueError) as exc:
            print(f"Error: --rules: {exc}", file=sys.stderr)
            return 1
        proxy = _maybe_build_proxy(session, args, triggers)
        mirrors = _maybe_build_pty_proxy(session, args)

        log_dir = os.path.normpath(os.path.dirname(recorder.raw_path)) if recorder is not None else None
        if log_dir:
            print(f"Recording to {log_dir}", file=sys.stderr)

        plugins.start()
        if proxy is not None:
            proxy.start()
            # Reflect the port actually bound: with --listen-port 0 the kernel picks
            # one, and everything downstream (the banner, a daemon's state file) has
            # to report where clients can really reach us.
            args.listen_port = proxy.port
            print(f"Proxy listening on {args.listen}:{proxy.port}", file=sys.stderr)
            if args.listen in ("0.0.0.0", "::", ""):
                print("  (every interface — reachable from the network; "
                      "--listen 127.0.0.1 keeps it on this machine)", file=sys.stderr)
        if mirrors is not None:
            session.bus.subscribe(mirrors.handle)
            mirrors.start()
            _report_mirrors(mirrors, session.source.description())

        # A daemon registers itself (cmd_start); anything else serving does it here.
        if proxy is not None and not getattr(args, "daemon", False):
            registered = register_foreground(args, proxy, log_dir)
            if registered is not None:
                print(f"Registered as '{registered.name}' — "
                      f"'uart-proxy status --show-auth' shows the code again",
                      file=sys.stderr)
        info_lines = session_info_lines(proxy, listen=args.listen if proxy else "",
                                        registered=registered, mirrors=mirrors,
                                        log_dir=log_dir)

        # Everything a client needs is now in place (listening socket, mirrors), so
        # a detached start can report success — before the device is necessarily
        # present, since waiting for one is normal and not a failure to launch.
        if on_ready is not None:
            on_ready()

        if args.no_tui:
            from .ui.headless import run_headless

            run_headless(session, ts_mode=args.timestamp,
                         quiet=getattr(args, "daemon", False),
                         history=history() if history else None)
        else:
            from .ui.tui import run_tui

            run_tui(session, title=title, ts_mode=args.timestamp, log_hint=log_dir,
                    history=history() if history else None,
                    prefix=prefix, input_mode=getattr(args, "input", None) or "line",
                    detachable=detachable, info=info_lines,
                    serve_hint=(f"{args.listen}:{proxy.port}"
                                if proxy is not None else None),
                    mirror_stats=mirrors.stats if mirrors is not None else None)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if registered is not None:
            registered.remove()
        if restore_term is not None:
            restore_term()
        if mirrors is not None:
            mirrors.stop()
        if proxy is not None:
            proxy.stop()
        if plugins is not None:
            plugins.stop()
        session.stop()
        if triggers is not None:
            triggers.close()
        if recorder is not None:
            recorder.mark(session_footer(session))
            written = close_recorder(recorder)
            if written:
                print("Logs written:", file=sys.stderr)
                for path in written:
                    print(f"  {path}", file=sys.stderr)
    return 0


# ── connect (local UART) ──────────────────────────────────────────────────────


def cmd_connect(args: argparse.Namespace,
                on_ready: Optional[Callable[[], None]] = None) -> int:
    error = apply_profile(args)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    port = resolve_port(args)
    if port is None:
        return 1
    args.port = port
    config = _build_config(args)
    if is_port_url(args.port):
        # A serial port on the network (SPEC S34): console server, ser2net, QEMU.
        error = check_port_url(args.port)
        if error:
            print(f"Error: {error}", file=sys.stderr)
            return 1
        if args.port.startswith(("ssh://", "telnet://")):
            source = (_ssh_source(args) if args.port.startswith("ssh://")
                      else _telnet_source(args))
            if source is None:
                return 1
            # A shell or a BBS on the far end reads keys, not lines.
            if getattr(args, "input", None) is None:
                args.input = "char"
        else:
            source = UrlSource(args.port, config)
    else:
        source = UartSource(args.port, config, exclusive=not args.no_exclusive)
    session = UartSession(
        source,
        encoding=args.encoding,
        default_eol=_EOL_MAP[args.eol],
        auto_reconnect=not args.no_reconnect,
        reconnect_interval=args.reconnect_interval,
    )
    if isinstance(source, (UrlSource, SshSource, TelnetSource)):
        attach_network_note(session, source)
    else:
        attach_exclusivity_report(session, source, requested=not args.no_exclusive)
        attach_busy_report(session, source)
        attach_move_report(session, source)
    return _run_session(session, args, title=f"uart-proxy · {args.port}",
                        on_ready=on_ready)


# ── start / status / stop (background sessions) ───────────────────────────────


def cmd_start(args: argparse.Namespace) -> int:
    """Run a session detached, so it outlives this terminal."""
    if not DAEMON_SUPPORTED:
        print("Error: background sessions need POSIX fork/setsid; "
              "use 'uart-proxy connect' in the foreground.", file=sys.stderr)
        return 1

    error = apply_profile(args)
    if error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    # Nobody to ask once detached, and asking before would surprise a script:
    # the port is --port, or the profile's one match.
    if not args.port and not getattr(args, "profile_rules", None):
        print("Error: start needs --port, or a --profile whose rules match a "
              "connected port.", file=sys.stderr)
        return 1
    port = resolve_port(args, allow_picker=False)
    if port is None:
        return 1
    args.port = port

    prune_dead()
    name = args.name or device_stem(args.port)
    try:
        existing = find_daemon(name)
    except DaemonNotFound:
        existing = None
    if existing is not None:
        print(f"Error: '{name}' is already running (pid {existing.pid}). "
              f"Attach to it, or stop it with 'uart-proxy stop {name}'.",
              file=sys.stderr)
        return 1
    # A second daemon on a port the first one holds would sit in "waiting"
    # forever, and succeed only in splitting the stream if the first opted out
    # of the exclusive claim — so say where the port already is instead.
    holder = next((d for d in list_daemons() if same_device(d.port, args.port)),
                  None)
    if holder is not None:
        print(f"Error: {args.port} is already held by '{holder.name}' "
              f"(pid {holder.pid}). Join it with 'uart-proxy attach "
              f"{holder.name}', or stop it with 'uart-proxy stop {holder.name}'.",
              file=sys.stderr)
        return 1

    # Resolve where things land *before* detaching, so the state file can point
    # at them and the parent can report them.
    log_dir = None if args.no_log else _resolve_output_dir(args)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    specs = resolve_auth_specs(args) or [new_auth_code()]
    codes = {code: role.value for code, role in map(parse_auth_spec, specs)}
    auth = _best_code(codes)

    # A daemon nobody can reach is useless, so it always serves — on loopback
    # unless asked otherwise, because a background process quietly listening on
    # every interface is not what anyone meant by "run it in the background".
    args.serve = True
    args.auth = specs
    args.no_tui = True
    args.daemon = True

    info = DaemonInfo(
        name=name, pid=0, port=args.port, baud=args.baud,
        listen_host=args.listen, listen_port=args.listen_port, auth=auth,
        log_dir=log_dir, proxy_dir=args.proxy_dir, started_at=time.time(),
        version=__version__, codes=codes,
    )

    daemon_log = os.path.join(log_dir, "daemon.log") if log_dir else None

    def report_started() -> None:
        """Printed by the parent, only once the child says it is up."""
        if args.quiet:
            return
        # The port actually bound is only known in the child; it records it in
        # the state file before saying it is up, so `--listen-port 0` reads back
        # as the kernel's choice rather than as 0.
        written = read_state(info.path)
        port = written.listen_port if written is not None else args.listen_port
        print(f"Started '{name}' — {args.port} @ {args.baud}", file=sys.stderr)
        print(f"  proxy    {args.listen}:{port} (auth in {info.path})",
              file=sys.stderr)
        if log_dir:
            print(f"  logs     {log_dir}", file=sys.stderr)
        if args.proxy_dir or args.proxy:
            print(f"  mirrors  {args.proxy_dir or 'see --proxy'}", file=sys.stderr)
        print(f"  stop it  uart-proxy stop {name}", file=sys.stderr)

    # From here the parent never returns: it waits for the child's verdict and
    # exits with it, so a daemon that cannot open its port is a failed command.
    signal_ready = daemonize(stderr_path=daemon_log, on_started=report_started)

    info.pid = os.getpid()
    try:
        info.write()
    except OSError as exc:
        signal_ready(f"could not write {info.path}: {exc}")
        return 1

    def ready() -> None:
        # The proxy is listening now, so the state file can record the port it
        # actually got — which is the only way `--listen-port 0` can work for a
        # daemon a client has to find later.
        info.listen_port = args.listen_port
        try:
            info.write()
        except OSError:
            pass
        signal_ready(None)

    try:
        result = cmd_connect(args, on_ready=ready)
    except Exception as exc:  # noqa: BLE001 - report it to the parent, not a tty
        signal_ready(str(exc))
        raise
    finally:
        info.remove()
    return result


def _format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"
    return f"{int(seconds // 86400)}d{int((seconds % 86400) // 3600):02d}h"


def cmd_status(args: argparse.Namespace) -> int:
    """Show the sessions that are running — background, or serving in a terminal."""
    prune_dead()
    try:
        daemons = [find_daemon(args.name)] if args.name else list_daemons()
    except DaemonNotFound as exc:
        if args.json:
            print(json.dumps({"status": False, "action": "status", "error": str(exc)}))
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps({
            "status": True, "action": "status",
            "data": [
                {
                    "name": d.name, "pid": d.pid, "port": d.port, "baud": d.baud,
                    "listen": f"{d.listen_host}:{d.listen_port}",
                    "log_dir": d.log_dir, "proxy_dir": d.proxy_dir,
                    "started_at": d.started_at, "uptime": d.uptime,
                    "last_activity": d.last_activity(), "version": d.version,
                    "foreground": d.foreground, "owner": d.owner, "title": d.title,
                    **({"auth": _status_codes(d)} if args.show_auth else {}),
                }
                for d in daemons
            ],
        }))
        return 0

    if not daemons:
        print("No session is running.")
        print("Start one with:  uart-proxy start --port /dev/cu.usbserial-110")
        return 0

    now = time.time()
    print(f"{'NAME':16s} {'PID':>7s} {'UP':>7s} {'QUIET':>7s}  PORT")
    for d in daemons:
        last = d.last_activity()
        quiet_for = (now - last) if last else None
        fg = "  (foreground)" if d.foreground else ""
        print(f"{d.name:16s} {d.pid:7d} {_format_age(d.uptime):>7s} "
              f"{_format_age(quiet_for):>7s}  {d.port} @ {d.baud}{fg}")
    print()
    for d in daemons:
        print(f"{d.name}:")
        print(f"  proxy    {d.listen_host}:{d.listen_port}")
        if args.show_auth:
            for code, role in _status_codes(d).items():
                print(f"  auth     {code}  ({role})")
        if d.log_dir:
            print(f"  logs     {d.log_dir}")
        if d.proxy_dir:
            print(f"  mirrors  {d.proxy_dir}")
        print(f"  state    {d.path}")
    if not args.show_auth:
        print("(auth codes hidden — add --show-auth)")
    return 0


def _status_codes(d: DaemonInfo) -> dict[str, str]:
    # State files from before codes were recorded carry only the one code.
    return dict(d.codes) or ({d.auth: Role.FULL.value} if d.auth else {})


def cmd_stop(args: argparse.Namespace) -> int:
    """Ask a background session to shut down."""
    prune_dead()
    if args.all:
        daemons = list_daemons()
        if not daemons:
            print("No background session is running.")
            return 0
    else:
        try:
            daemons = [find_daemon(args.name)]
        except DaemonNotFound as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

    failed = False
    for info in daemons:
        outcome = stop_daemon(info, force=args.force)
        print(outcome)
        failed |= "still running" in outcome or "not ours" in outcome
    return 1 if failed else 0


# ── attach (client of a local background session) ─────────────────────────────


def _adopt_remote_timeline(session: UartSession, source: SocketSource) -> None:
    """Share the server's elapsed axis instead of starting a second one.

    Otherwise replayed history is measured from when the *server's* session
    started while live output is measured from when *this* client connected, and
    the elapsed column jumps backwards exactly where replay ends. Adopting the
    server's origin also makes a line's elapsed value mean the same thing here as
    in the server's own log files, which is what you want when cross-referencing.
    (This settles the question ROADMAP left open: replay forces the answer.)
    """
    if source.remote_elapsed is not None:
        session.tracker.rebase(source.remote_elapsed)


def cmd_attach(args: argparse.Namespace) -> int:
    """Attach to a background session, showing what happened while you were away."""
    prune_dead()
    try:
        info = find_daemon(args.name)
    except DaemonNotFound as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # A client, not a second owner: the daemon holds the port exclusively (S15),
    # so attaching means speaking its protocol — which is also what lets several
    # people attach at once.
    host = connect_host(info.listen_host)
    source = SocketSource(
        host, info.listen_port, info.auth,
        replay_lines=args.replay_lines,
    )
    # Connect here rather than leaving it to the session's background manager:
    # the history has to be in hand before the UI draws its first frame. It also
    # makes an unreachable session an error you see now, instead of a view stuck
    # in "waiting" for a daemon that is answering somewhere else. open() is
    # idempotent, so the manager's own call is a no-op.
    try:
        source.open()
    except Exception as exc:  # noqa: BLE001
        print(f"Error: cannot reach '{info.name}' at "
              f"{host}:{info.listen_port}: {exc}", file=sys.stderr)
        return 1

    session = UartSession(
        source,
        encoding=args.encoding,
        default_eol=_EOL_MAP[args.eol],
        auto_reconnect=not args.no_reconnect,
        reconnect_interval=args.reconnect_interval,
    )
    _adopt_remote_timeline(session, source)
    source.on_tx_echo = session.publish_remote_tx
    args.serve = False
    if not args.mirror_stem:
        # Not `<name>-0`: those are the daemon's own mirrors, maybe in the same dir.
        args.mirror_stem = f"{info.name}-attach"
    # The daemon is already recording this session; a second copy under the
    # client would only duplicate it.
    args.no_log = True
    return _run_session(
        session, args, title=f"uart-proxy · {info.name} (attached)",
        history=lambda: source.replay, detachable=True,
    )


# ── remote (socket client) ────────────────────────────────────────────────────


def cmd_replay(args: argparse.Namespace) -> int:
    """Play a recorded session back at its own pace (SPEC S37)."""
    from .core.recording import load, resolve

    try:
        recording = load(resolve(args.path, DEFAULT_LOG_ROOT))
    except (FileNotFoundError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    ok, size = _term_size(args)
    if not ok:
        return 1
    if not recording.has_timing:
        print(f"Note: no timing file beside {recording.raw_path} — it plays "
              f"all at once (recorded before timing was kept?)", file=sys.stderr)
    start_at = 0.0
    if args.at:
        from .core.recording import parse_at

        try:
            start_at = parse_at(args.at, recording)
        except ValueError as exc:
            print(f"Error: --at: {exc}", file=sys.stderr)
            return 1
        if not 0 <= start_at <= recording.duration:
            print(f"Error: --at {args.at!r} is outside the recording "
                  f"(which lasts {recording.duration:.0f} s)", file=sys.stderr)
            return 1
    if args.no_tui:
        from .ui.replay import play_to_stream, stdout_bytes

        try:
            play_to_stream(recording, stdout_bytes(), speed=args.speed,
                           max_idle=args.max_idle, start_at=start_at)
        except (BrokenPipeError, KeyboardInterrupt):
            pass
        return 0
    from .ui.replay import run_replay

    run_replay(recording, speed=args.speed, max_idle=args.max_idle, size=size,
               start_at=start_at)
    return 0


def cmd_remote(args: argparse.Namespace) -> int:
    source = SocketSource(args.host, args.port, args.auth,
                          replay_lines=getattr(args, "replay_lines", 0))
    session = UartSession(
        source,
        encoding=args.encoding,
        default_eol=_EOL_MAP[args.eol],
        auto_reconnect=not args.no_reconnect,
        reconnect_interval=args.reconnect_interval,
    )
    # A remote client never re-serves by default; ignore --serve here.
    try:
        source.open()   # eager, so the timeline (and any replay) is in hand
    except Exception as exc:  # noqa: BLE001
        print(f"Error: cannot reach {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 1
    _adopt_remote_timeline(session, source)
    source.on_tx_echo = session.publish_remote_tx
    args.serve = False
    if not args.mirror_stem:
        args.mirror_stem = f"{args.host}-{args.port}".replace(":", "_").replace("/", "_")
    return _run_session(session, args,
                        title=f"uart-proxy · {args.host}:{args.port}",
                        history=lambda: source.replay)


# ── argument parsing ──────────────────────────────────────────────────────────


def _add_client_mirror_args(p: argparse.ArgumentParser, *, default_name: str) -> None:
    """PTY mirrors for a client (``remote``, ``attach``) — SPEC S33.

    The same mirrors ``connect`` offers, fed from the stream we are a client
    of: a remote port becomes a local PTY that ``screen`` or a script can open
    as though the device were plugged in here.
    """
    p.add_argument("--proxy-dir", nargs="?", const=DEFAULT_PROXY_DIR, default=None,
                   metavar="DIR",
                   help=f"Expose the stream as local read/write PTY mirrors, "
                        f"symlinked into DIR (default {DEFAULT_PROXY_DIR}), named "
                        f"{default_name}-0, -1…")
    p.add_argument("--proxy-count", type=int, default=DEFAULT_PROXY_COUNT, metavar="N",
                   help=f"How many mirrors (default {DEFAULT_PROXY_COUNT}).")
    p.add_argument("--proxy", action="append", metavar="PATH",
                   help="Expose a mirror at exactly PATH (repeatable).")
    p.add_argument("--tx-merge", choices=list(TX_MERGE_MODES), default=DEFAULT_TX_MERGE,
                   help="How mirror writers reach the wire (see 'connect --help').")
    p.add_argument("--proxy-max-lag", type=float, default=DEFAULT_MAX_LAG,
                   metavar="SECONDS", help=argparse.SUPPRESS)
    p.add_argument("--mirror-name", dest="mirror_stem", default=None, metavar="NAME",
                   help=f"Name the mirrors NAME-0, NAME-1… (default {default_name}).")


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
    p.add_argument("--log-rotate-mb", type=float, default=0, metavar="MB",
                   help="Split the logs into parts (output.001.log, …) once a "
                        "file passes MB. 0 = never (default).")
    p.add_argument("--log-keep-parts", type=int, default=0, metavar="N",
                   help="With --log-rotate-mb, delete the oldest parts beyond N "
                        "— this is what caps a session that never ends. "
                        "0 = keep all (default).")
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
    # triggers (S46)
    p.add_argument("--rules", action="append", metavar="FILE",
                   help="Load trigger rules from a JSON file (repeatable): when the device "
                        "says X, mark, notify or send Y. Rules that send stay off unless "
                        "--approve-rules.")
    p.add_argument("--approve-rules", action="store_true",
                   help="Let the rules that send, as loaded from --rules, act.")


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
    p_conn.add_argument("--port", default=None,
                        help="Serial device path or COM port — or a network port: "
                             "socket://HOST:PORT (raw TCP), rfc2217://HOST:PORT "
                             "(console servers, ser2net, qemu), "
                             "ssh://[USER@]HOST[:PORT] or telnet://HOST[:PORT]. "
                             "Left out in a "
                             "terminal, you choose from a list.")
    p_conn.add_argument("--ssh-command", metavar="CMD", default=None,
                        help="With --port ssh://…: run CMD on the far end instead "
                             "of a login shell, e.g. \"picocom -b 115200 /dev/ttyUSB0\".")
    p_conn.add_argument("--ssh-option", action="append", metavar="OPT",
                        help="With --port ssh://…: pass -o OPT to ssh (repeatable).")
    p_conn.add_argument("--term-size", metavar="COLSxROWS", default=None,
                        help="With --port ssh:// or telnet://: a fixed window size for the far "
                             "end, e.g. 80x24 for a BBS, or 'auto' (the default): "
                             "follow the terminal view — or, with --no-tui, the "
                             "terminal itself.")
    p_conn.add_argument("--profile", metavar="NAME|FILE.toml", default=None,
                        help="A uart_helper device profile: its [defaults] set "
                             "baud/bytesize/parity/stopbits/flow control where "
                             "no flag does, and its [[rules]] find the port "
                             "when --port is left out.")
    # None = not given, so a profile can fill it (apply_profile).
    p_conn.add_argument("--baud", type=int, default=None, help="Baud rate. Default 115200.")
    p_conn.add_argument("--bytesize", type=int, default=None, choices=[5, 6, 7, 8],
                        help="Default 8.")
    p_conn.add_argument("--parity", default=None, choices=["N", "E", "O", "M", "S"],
                        help="Default N.")
    p_conn.add_argument("--stopbits", type=float, default=None, choices=[1, 1.5, 2],
                        help="Default 1.")
    p_conn.add_argument("--serve", action="store_true",
                        help="Also expose this session via a socket proxy.")
    p_conn.add_argument("--listen", default="0.0.0.0", help="Proxy bind address.")
    p_conn.add_argument("--listen-port", type=int, default=9600, help="Proxy port.")
    p_conn.add_argument("--echo-tx", action="store_true",
                        help="Show proxy clients each line typed into the device "
                             "(by anyone). Off by default: that includes a "
                             "password typed at a login prompt.")
    p_conn.add_argument("--max-watches", type=int, default=3, metavar="N",
                        help="Watches each proxy client (an AI tool) may ask for "
                             "(S47): they only report what they see. 0 = none. Default 3.")
    p_conn.add_argument("--max-clients", type=int, default=MAX_CLIENTS,
                        help=f"Proxy connections served at once; more are told "
                             f"to retry. 0 = no cap. Default {MAX_CLIENTS}.")
    p_conn.add_argument("--auth", action="append", metavar="CODE[:role]",
                        help="Auth code, optional role (full|readonly). Repeatable. "
                             "Without it --serve generates a random full-access "
                             "code for the run and prints it.")
    p_conn.add_argument("--no-exclusive", action="store_true",
                        help="Don't claim the port exclusively (TIOCEXCL). By "
                             "default nothing else on this machine can open it, "
                             "so two programs can't silently split the stream.")
    # local PTY mirrors (POSIX)
    p_conn.add_argument("--proxy-dir", nargs="?", const=DEFAULT_PROXY_DIR, default=None,
                        metavar="DIR",
                        help="Share this port as read/write PTY mirrors, "
                             f"symlinked into DIR (default {DEFAULT_PROXY_DIR}). "
                             "Any tool (screen, minicom, pyserial) can then "
                             "attach while uart-proxy keeps the real port.")
    p_conn.add_argument("--proxy-count", type=int, default=DEFAULT_PROXY_COUNT,
                        metavar="N",
                        help=f"How many mirrors to expose (default "
                             f"{DEFAULT_PROXY_COUNT}). Each is full-duplex, so N "
                             f"mirrors means N readers and N writers.")
    p_conn.add_argument("--proxy", action="append", metavar="PATH",
                        help="Expose a mirror at exactly PATH (repeatable); "
                             "use instead of / alongside --proxy-dir.")
    p_conn.add_argument("--tx-merge", choices=list(TX_MERGE_MODES),
                        default=DEFAULT_TX_MERGE,
                        help="How mirror writers reach the one wire. "
                             "raw (default) = every byte crosses immediately, so "
                             "^C, tab completion and arrow keys work — what an "
                             "attached tool expects of a serial port. "
                             "line = hold each writer's bytes until a line "
                             "ending so concurrent commands can't interleave, at "
                             "the cost of all of that.")
    p_conn.add_argument("--proxy-max-lag", type=float, default=DEFAULT_MAX_LAG,
                        metavar="SECONDS",
                        help="Discard a mirror's backlog after SECONDS with no "
                             f"reader taking it (default {DEFAULT_MAX_LAG:g}; 0 = "
                             "keep it). A mirror is a live view — handing stale "
                             "output to the next tool that opens it makes a "
                             "program read the past as the present. History lives "
                             "in the log files.")
    p_conn.add_argument("--replay-lines", type=int, default=DEFAULT_REPLAY_LINES,
                        metavar="N",
                        help="Keep the last N device lines so an attaching client "
                             f"can see what it missed (default {DEFAULT_REPLAY_LINES}; "
                             "0 = no history).")
    p_conn.add_argument("--prefix", default=DEFAULT_PREFIX, metavar="KEY",
                       help="The TUI's command prefix, e.g. 'ctrl+]' (default) or "
                            "'ctrl+a'. Every other key can then go to the device; "
                            "press it twice to send it literally.")
    p_conn.add_argument("--input", choices=("line", "char"), default=None,
                       help="Start in line mode (type a command, press Enter) or "
                            "character mode (every keystroke goes straight to the "
                            "device, so ^C and tab completion work). Default line; "
                            "char for ssh://.")
    _add_common_io_args(p_conn)
    p_conn.set_defaults(func=cmd_connect)

    # start (detached connect)
    p_start = sub.add_parser(
        "start",
        help="Run a session in the background, so it outlives this terminal.",
        description="Same as 'connect', but detached: the engine keeps the port, "
                    "keeps recording and keeps serving after you close the "
                    "terminal. It always serves the proxy (on loopback unless "
                    "--listen says otherwise) with a generated auth code, so a "
                    "client can reach it later. See 'status' and 'stop'.",
    )
    p_start.add_argument("--port", default=None,
                         help="Serial device path, socket://HOST:PORT or "
                              "rfc2217://HOST:PORT (or let --profile find it).")
    p_start.add_argument("--name", default=None,
                         help="Name this session (default: the device stem, e.g. "
                              "usbserial-110).")
    p_start.add_argument("--ssh-command", metavar="CMD", default=None,
                        help="With --port ssh://…: run CMD on the far end instead "
                             "of a login shell, e.g. \"picocom -b 115200 /dev/ttyUSB0\".")
    p_start.add_argument("--ssh-option", action="append", metavar="OPT",
                        help="With --port ssh://…: pass -o OPT to ssh (repeatable).")
    p_start.add_argument("--term-size", metavar="COLSxROWS", default=None,
                        help="With --port ssh://…: a fixed window size for the far "
                             "end, e.g. 80x24 for a BBS. Default: follow the "
                             "terminal view.")
    p_start.add_argument("--profile", metavar="NAME|FILE.toml", default=None,
                        help="A uart_helper device profile: its [defaults] set "
                             "baud/bytesize/parity/stopbits/flow control where "
                             "no flag does, and its [[rules]] find the port "
                             "when --port is left out.")
    # None = not given, so a profile can fill it (apply_profile).
    p_start.add_argument("--baud", type=int, default=None, help="Baud rate. Default 115200.")
    p_start.add_argument("--bytesize", type=int, default=None, choices=[5, 6, 7, 8],
                        help="Default 8.")
    p_start.add_argument("--parity", default=None, choices=["N", "E", "O", "M", "S"],
                        help="Default N.")
    p_start.add_argument("--stopbits", type=float, default=None, choices=[1, 1.5, 2],
                        help="Default 1.")
    p_start.add_argument("--listen", default="127.0.0.1",
                         help="Proxy bind address. Default 127.0.0.1 — a "
                              "background process should not listen on every "
                              "interface unless you say so.")
    p_start.add_argument("--listen-port", type=int, default=9600, help="Proxy port.")
    p_start.add_argument("--echo-tx", action="store_true",
                         help="Show attached clients each line typed into the "
                              "device. Off by default (passwords are lines too).")
    p_start.add_argument("--max-watches", type=int, default=3, metavar="N",
                        help="Watches each proxy client (an AI tool) may ask for "
                             "(S47): they only report what they see. 0 = none. Default 3.")
    p_start.add_argument("--max-clients", type=int, default=MAX_CLIENTS,
                         help=f"Proxy connections served at once. Default {MAX_CLIENTS}.")
    p_start.add_argument("--auth", action="append", metavar="CODE[:role]",
                         help="Auth code (default: a generated one, stored in the "
                              "session's 0600 state file).")
    p_start.add_argument("--quiet", action="store_true",
                         help="Don't print the startup summary.")
    p_start.add_argument("--no-exclusive", action="store_true",
                         help="Don't claim the port exclusively (TIOCEXCL).")
    p_start.add_argument("--proxy-dir", nargs="?", const=DEFAULT_PROXY_DIR,
                         default=None, metavar="DIR",
                         help="Also expose read/write PTY mirrors in DIR "
                              f"(default {DEFAULT_PROXY_DIR}).")
    p_start.add_argument("--proxy-count", type=int, default=DEFAULT_PROXY_COUNT,
                         metavar="N", help=f"How many mirrors (default {DEFAULT_PROXY_COUNT}).")
    p_start.add_argument("--proxy", action="append", metavar="PATH",
                         help="Expose a mirror at exactly PATH (repeatable).")
    p_start.add_argument("--tx-merge", choices=list(TX_MERGE_MODES),
                         default=DEFAULT_TX_MERGE,
                         help="How mirror writers reach the wire. See 'connect --help'.")
    p_start.add_argument("--proxy-max-lag", type=float, default=DEFAULT_MAX_LAG,
                         metavar="SECONDS",
                        help="Discard a mirror's backlog after SECONDS with no "
                             f"reader taking it (default {DEFAULT_MAX_LAG:g}; 0 = "
                             "keep it). A mirror is a live view — handing stale "
                             "output to the next tool that opens it makes a "
                             "program read the past as the present. History lives "
                             "in the log files.")
    p_start.add_argument("--replay-lines", type=int, default=DEFAULT_REPLAY_LINES,
                        metavar="N",
                        help="Keep the last N device lines so an attaching client "
                             f"can see what it missed (default {DEFAULT_REPLAY_LINES}; "
                             "0 = no history).")
    _add_common_io_args(p_start)
    p_start.set_defaults(func=cmd_start)

    # status
    p_stat = sub.add_parser("status",
                            help="Show running sessions: background ones, and "
                                 "'connect --serve' in another terminal.")
    p_stat.add_argument("name", nargs="?", default=None,
                        help="Only this session (default: all).")
    p_stat.add_argument("--json", action="store_true", help="Emit JSON output.")
    p_stat.add_argument("--show-auth", action="store_true",
                        help="Also print each session's proxy auth codes.")
    p_stat.set_defaults(func=cmd_status)

    # stop
    p_stop = sub.add_parser("stop", help="Shut down a background session.")
    p_stop.add_argument("name", nargs="?", default=None,
                        help="Which session (default: the only one running).")
    p_stop.add_argument("--all", action="store_true", help="Stop every session.")
    p_stop.add_argument("--force", action="store_true",
                        help="SIGKILL instead of an ordered shutdown. Leaves the "
                             "mirror symlinks behind for the next start to clear.")
    p_stop.set_defaults(func=cmd_stop)

    # attach
    p_att = sub.add_parser(
        "attach",
        help="Attach to a background session (see 'start'), replaying what you missed.",
        description="Connects to a running background session as a client — the "
                    "daemon keeps the port. Host, port and auth code come from "
                    "the session's state file, and the session's recent history "
                    "is replayed first with its original timestamps, so you can "
                    "see what happened while nobody was watching.",
    )
    p_att.add_argument("name", nargs="?", default=None,
                       help="Which session (default: the only one running).")
    p_att.add_argument("--replay-lines", type=int, default=DEFAULT_REPLAY_LINES,
                       metavar="N",
                       help=f"Replay up to N past lines (default "
                            f"{DEFAULT_REPLAY_LINES}; 0 = live only).")
    p_att.add_argument("--prefix", default=DEFAULT_PREFIX, metavar="KEY",
                       help="The TUI's command prefix, e.g. 'ctrl+]' (default) or "
                            "'ctrl+a'. Every other key can then go to the device; "
                            "press it twice to send it literally.")
    p_att.add_argument("--input", choices=("line", "char"), default="line",
                       help="Start in line mode (type a command, press Enter) or "
                            "character mode (every keystroke goes straight to the "
                            "device, so ^C and tab completion work).")
    _add_client_mirror_args(p_att, default_name="<session>-attach")
    _add_common_io_args(p_att)
    p_att.set_defaults(func=cmd_attach)

    # replay
    p_play = sub.add_parser(
        "replay", help="Play a recorded session back, at its own pace.",
        description="Plays output.log back as it arrived (using output-timing.log), "
                    "through a terminal emulator — so a full-screen program, a "
                    "BBS or a boot menu reads as it looked. Space pauses, ←/→ "
                    "seek 5 s, +/- change speed, Home/End jump, g goes to a "
                    "time, q quits.")
    p_play.add_argument("path", nargs="?", default=None,
                        help="A session folder or an output*.log (default: the "
                             "newest recorded session).")
    p_play.add_argument("--at", metavar="TIME", default=None,
                        help="Start here: +00:12:40 (session elapsed, as the "
                             "timestamped logs show), 03:12:40 (time of day) or "
                             "'2026-09-30 03:12:40'. Everything before it is "
                             "drawn at once.")
    p_play.add_argument("--speed", type=float, default=1.0,
                        help="Playback speed (default 1; 4 = four times faster).")
    p_play.add_argument("--max-idle", type=float, default=2.0, metavar="SECONDS",
                        help="Cut silences longer than this short (default 2; "
                             "0 = keep them whole).")
    p_play.add_argument("--term-size", metavar="COLSxROWS", default=None,
                        help="Draw at a fixed size, e.g. 80x24 for a BBS "
                             "(default: the window).")
    p_play.add_argument("--no-tui", action="store_true",
                        help="Write the bytes to this terminal at their pace "
                             "instead, and let it draw them (like scriptreplay).")
    p_play.set_defaults(func=cmd_replay)

    # remote
    p_rem = sub.add_parser("remote", help="Attach to a remote uart-proxy server.")
    p_rem.add_argument("--host", required=True, help="Remote server address.")
    p_rem.add_argument("--port", type=int, default=9600, help="Remote server port.")
    p_rem.add_argument("--auth", required=True, help="Auth code for the remote server.")
    p_rem.add_argument("--replay-lines", type=int, default=0, metavar="N",
                       help="Ask the server to replay its last N lines first "
                            "(default 0; older servers simply send none).")
    p_rem.add_argument("--prefix", default=DEFAULT_PREFIX, metavar="KEY",
                       help="The TUI's command prefix, e.g. 'ctrl+]' (default) or "
                            "'ctrl+a'. Every other key can then go to the device; "
                            "press it twice to send it literally.")
    p_rem.add_argument("--input", choices=("line", "char"), default="line",
                       help="Start in line mode (type a command, press Enter) or "
                            "character mode (every keystroke goes straight to the "
                            "device, so ^C and tab completion work).")
    _add_client_mirror_args(p_rem, default_name="<host>-<port>")
    _add_common_io_args(p_rem)
    p_rem.set_defaults(func=cmd_remote)

    # tail / expect / send: a served session from a script (SPEC S41)
    from . import client_cli

    client_cli.add_parsers(sub)
    # mcp: an MCP server for AI tools (SPEC S42)
    from . import mcp

    mcp.add_parser(sub)

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
