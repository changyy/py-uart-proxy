"""
Background sessions — start detached, list, stop.

A serial session is long-lived: you want it to survive closing the terminal, and
you want to walk away and come back. That means the engine has to be a process of
its own, with the UI as a client — the same split tmux makes between its server
and `tmux attach`. This module owns that process's identity and lifecycle:

    uart-proxy start  --port /dev/cu.usbserial-110   # detach into the background
    uart-proxy status                                # what is running
    uart-proxy stop                                  # ordered shutdown

Each running daemon is described by one JSON file under
``~/.uart-proxy/daemons/<name>.json`` (0600 — it holds the proxy's auth code).
The file is the whole registry: no central index to get out of step, and a
crashed daemon leaves exactly one stale file, which the next command prunes.

``<name>`` defaults to the device stem, the same rule the PTY mirrors use
(``/dev/cu.usbserial-110`` → ``usbserial-110``), so one daemon per adapter reads
naturally and two adapters can run side by side.

Detaching needs ``fork`` + ``setsid``, so ``start`` is POSIX only; the registry
itself works on every OS (SPEC S39) — a foreground ``connect --serve``, or an
application serving a session, registers there on Windows too.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: Overrides the ``~/.uart-proxy`` root — set it to keep a test (or a second
#: install) out of the real one.
HOME_ENV = "UART_PROXY_HOME"

DAEMON_SUPPORTED = sys.platform != "win32" and hasattr(os, "fork")

#: How long `start` waits for the child to report that it is up.
READY_TIMEOUT = 20.0

#: How long `stop` waits for an ordered shutdown before saying so.
STOP_TIMEOUT = 10.0

_STATE_SUFFIX = ".json"


def uart_proxy_home() -> str:
    return os.path.normpath(
        os.environ.get(HOME_ENV) or os.path.expanduser("~/.uart-proxy")
    )


def daemon_dir() -> str:
    return os.path.join(uart_proxy_home(), "daemons")


def new_auth_code() -> str:
    """A per-daemon code, so `attach` needs no shared secret from the user.

    It lives in the 0600 state file; the daemon binds to loopback by default, so
    this guards against other *local* users, which is the threat that remains.
    """
    return secrets.token_hex(8)


@dataclass
class DaemonInfo:
    """Everything a client needs to find and describe a running daemon."""

    name: str
    pid: int
    port: str                    # the serial device it holds
    baud: int
    listen_host: str
    listen_port: int
    auth: str
    log_dir: Optional[str] = None
    proxy_dir: Optional[str] = None
    mirrors: list[str] = field(default_factory=list)
    started_at: float = 0.0      # epoch seconds
    version: str = ""
    #: Every code the proxy accepts, with its role. ``auth`` is the one
    #: ``attach`` uses; this is what ``status --show-auth`` lists.
    codes: dict[str, str] = field(default_factory=dict)
    #: A ``connect --serve`` in a terminal rather than a detached ``start``
    #: (SPEC S23): registered so ``status`` / ``attach`` can find it too.
    foreground: bool = False
    #: Who serves it (SPEC S39): ``uart-proxy``, or an embedding application.
    owner: str = "uart-proxy"
    #: What to call it, for a list a person reads (e.g. an app's tab title).
    title: str = ""

    # ── persistence ────────────────────────────────────────────────────────

    @property
    def path(self) -> str:
        return state_path(self.name)

    def write(self) -> None:
        """Persist atomically, 0600 — the file carries the auth code."""
        directory = daemon_dir()
        os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = f"{self.path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, sort_keys=True)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            if sys.platform != "win32":
                raise
            # Windows: the profile's permissions keep other users out (S39).
        os.replace(tmp, self.path)

    def remove(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.debug("could not remove %s", self.path, exc_info=True)

    # ── liveness ───────────────────────────────────────────────────────────

    @property
    def is_alive(self) -> bool:
        """Whether the recorded pid still exists.

        Signal 0 asks the kernel without delivering anything. A recycled pid
        would fool this; pids recycle slowly enough, and the cost of being wrong
        is a confusing message rather than damage.
        """
        if self.pid <= 0:
            return False
        if sys.platform == "win32":
            return _windows_pid_alive(self.pid)
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by someone else
        return True

    @property
    def uptime(self) -> float:
        return max(0.0, time.time() - self.started_at) if self.started_at else 0.0

    def last_activity(self) -> Optional[float]:
        """Epoch seconds of the newest byte recorded, if it is recording.

        The daemon's live counters are in its own memory, but the log file's
        mtime is on disk and answers the question that actually matters when you
        run `status`: is this thing still hearing anything?
        """
        if not self.log_dir:
            return None
        try:
            newest = max(
                os.path.getmtime(os.path.join(self.log_dir, name))
                for name in os.listdir(self.log_dir)
            )
        except (OSError, ValueError):
            return None
        return newest

    @classmethod
    def from_dict(cls, data: dict) -> "DaemonInfo":
        known = {f for f in cls.__dataclass_fields__}  # tolerate older/newer files
        return cls(**{k: v for k, v in data.items() if k in known})


def _kernel32():
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5


def _windows_pid_alive(pid: int) -> bool:
    """Whether ``pid`` is a running process, asked without signalling it.

    ``os.kill(pid, 0)`` on Windows sends CTRL_C_EVENT — signal 0 is its value —
    so the probe asks for a query handle instead (SPEC S39).
    """
    import ctypes

    kernel = _kernel32()
    handle = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return kernel.GetLastError() == _ERROR_ACCESS_DENIED  # exists, not ours
    try:
        code = ctypes.c_ulong(0)
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == _STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


def codes_by_role(auth: dict) -> dict[str, str]:
    """A server's auth table as ``{code: role}`` strings, for the state file."""
    return {code: getattr(role, "value", str(role)) for code, role in auth.items()}


def best_code(codes: dict[str, str]) -> str:
    """The code ``attach`` uses: the full-access one when there is one."""
    for code, role in codes.items():
        if role == "full":
            return code
    return next(iter(codes), "")


def register_served(server, *, name: str, port: str, baud: int,
                    owner: str = "uart-proxy", title: str = "",
                    log_dir: Optional[str] = None, proxy_dir: Optional[str] = None,
                    version: str = "") -> Optional[DaemonInfo]:
    """Put a running ``ProxyServer`` in the registry, on any OS (SPEC S39).

    For a foreground ``connect --serve`` and for an application that serves a
    session of its own. ``name`` is a base: a live session using it gets
    ``-2``, ``-3``… Never raises: a registry that cannot be written is a note
    on stderr, and the session goes on being served.
    """
    codes = codes_by_role(server.auth)
    try:
        prune_dead()
        info = DaemonInfo(
            name=unique_name(name), pid=os.getpid(), port=port, baud=baud,
            listen_host=server.host, listen_port=server.port,
            auth=best_code(codes), codes=codes, log_dir=log_dir, proxy_dir=proxy_dir,
            started_at=time.time(), version=version, foreground=True,
            owner=owner, title=title,
        )
        info.write()
    except OSError as exc:
        print(f"Note: not registered for 'uart-proxy status' ({exc})", file=sys.stderr)
        return None
    return info


def state_path(name: str) -> str:
    return os.path.join(daemon_dir(), f"{name}{_STATE_SUFFIX}")


def read_state(path: str) -> Optional[DaemonInfo]:
    try:
        with open(path, encoding="utf-8") as fh:
            return DaemonInfo.from_dict(json.load(fh))
    except (OSError, ValueError, TypeError):
        logger.debug("unreadable daemon state %s", path, exc_info=True)
        return None


def list_daemons(*, include_dead: bool = False) -> list[DaemonInfo]:
    """Every daemon with a state file, newest first. Dead ones are hidden."""
    directory = daemon_dir()
    found: list[DaemonInfo] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    for entry in names:
        if not entry.endswith(_STATE_SUFFIX):
            continue
        info = read_state(os.path.join(directory, entry))
        if info is None:
            continue
        if include_dead or info.is_alive:
            found.append(info)
    return sorted(found, key=lambda d: d.started_at, reverse=True)


def prune_dead() -> list[DaemonInfo]:
    """Drop state files whose process is gone; return what was cleaned up.

    A daemon killed with ``SIGKILL`` cannot tidy up after itself (SPEC S16), so
    somebody has to, and the next command to look is the natural place.
    """
    removed = []
    for info in list_daemons(include_dead=True):
        if not info.is_alive:
            info.remove()
            removed.append(info)
    return removed


class DaemonNotFound(Exception):
    """No daemon matched — carries a message worth showing the user verbatim."""


def unique_name(base: str) -> str:
    """``base``, or ``base-2``, ``base-3``… — the first no live session uses."""
    taken = {d.name for d in list_daemons()}
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}-{n}"
    return name


def connect_host(listen_host: str) -> str:
    """Where a local client should connect to reach a server bound here.

    A wildcard bind is reachable on loopback, and connecting *to* ``0.0.0.0``
    is not portable, so translate it.
    """
    if listen_host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if listen_host == "::":
        return "::1"
    return listen_host


def find_daemon(name: Optional[str] = None) -> DaemonInfo:
    """Resolve a name to a running daemon.

    With no name: the only running one. Being explicit beats guessing when
    several are up, so that case lists them and fails.
    """
    running = list_daemons()
    if name:
        for info in running:
            if info.name == name:
                return info
        known = ", ".join(d.name for d in running) or "none running"
        raise DaemonNotFound(f"no running session named {name!r} ({known})")
    if not running:
        raise DaemonNotFound(
            "no session is running — start one with 'uart-proxy start --port …'"
        )
    if len(running) > 1:
        names = ", ".join(d.name for d in running)
        raise DaemonNotFound(f"several sessions are running; name one of: {names}")
    return running[0]


# ── detaching ───────────────────────────────────────────────────────────────


def daemonize(*, stderr_path: Optional[str] = None,
              on_started: Optional[Callable[[], None]] = None,
              ready_timeout: float = READY_TIMEOUT) -> Callable[[Optional[str]], None]:
    """Detach into the background, and hand back a way to report readiness.

    **In the parent this never returns**: it waits for the child to say whether
    it came up, then exits — so ``start`` failing to open a port is a failed
    command, not a silent success with a dead daemon behind it. ``on_started`` is
    called there, and only on success, so the summary a user reads is never an
    optimistic one printed before the outcome was known.

    In the child it returns ``signal_ready(error=None)``. Call it with None once
    the daemon is serving, or with a message if it cannot start.

    The double fork is the usual recipe: the first detaches from the shell's job
    control, ``setsid`` leaves the session and drops the controlling terminal,
    and the second makes re-acquiring one impossible. Do this **before** starting
    any threads — forking a threaded process is not safe.
    """
    if not DAEMON_SUPPORTED:
        raise RuntimeError(
            "background sessions need POSIX fork/setsid (not available here); "
            "run 'uart-proxy connect' in the foreground instead"
        )

    read_fd, write_fd = os.pipe()

    if os.fork() > 0:  # ── original process: wait for the verdict, then exit
        os.close(write_fd)
        message = _await_ready(read_fd, ready_timeout)
        os.close(read_fd)
        try:
            os.wait()  # reap the intermediate child
        except ChildProcessError:
            pass
        if message == "":
            if on_started is not None:
                on_started()
            raise SystemExit(0)
        if message is None:
            print("Timed out waiting for the background session to start.",
                  file=sys.stderr)
        else:
            print(f"Could not start the background session: {message}",
                  file=sys.stderr)
        raise SystemExit(1)

    os.close(read_fd)
    os.setsid()
    if os.fork() > 0:  # ── intermediate: its only job was to be reaped
        os._exit(0)

    # ── the daemon itself
    os.chdir("/")  # never pin a mount point we happen to have been launched from
    _redirect_std_fds(stderr_path)

    def signal_ready(error: Optional[str] = None) -> None:
        try:
            os.write(write_fd, (error or "").encode()[:4000] + b"\n")
            os.close(write_fd)
        except OSError:
            pass

    return signal_ready


def _await_ready(fd: int, timeout: float) -> Optional[str]:
    """Read the child's verdict: "" = up, text = its error, None = timed out."""
    import selectors

    deadline = time.monotonic() + timeout
    buffer = b""
    with selectors.DefaultSelector() as sel:
        sel.register(fd, selectors.EVENT_READ)
        while b"\n" not in buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            if not sel.select(remaining):
                return None
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:  # child died without a word
                return buffer.decode(errors="replace").strip() or "it exited immediately"
            buffer += chunk
    return buffer.split(b"\n", 1)[0].decode(errors="replace")


def _redirect_std_fds(stderr_path: Optional[str]) -> None:
    """Detach the standard streams from the terminal we just left.

    stdout goes nowhere: the data stream belongs in the recorder's log files, not
    duplicated into a second one. stderr is kept — the startup banner, the
    exclusivity report and any notices are how you diagnose a daemon that isn't
    doing what you expected.
    """
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    os.dup2(devnull, 1)
    if stderr_path:
        try:
            err = os.open(stderr_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            os.dup2(err, 2)
            os.close(err)
        except OSError:
            os.dup2(devnull, 2)
    else:
        os.dup2(devnull, 2)
    if devnull > 2:
        os.close(devnull)


def stop_daemon(info: DaemonInfo, *, timeout: float = STOP_TIMEOUT,
                force: bool = False) -> str:
    """Ask a daemon to shut down; return a one-line outcome.

    SIGTERM, because that is now an ordered shutdown in every mode (SPEC S16):
    the mirrors' symlinks come out, proxy clients are closed, log files are
    flushed and closed. ``force`` escalates to SIGKILL, which strands the
    symlinks — the next start clears them.
    """
    sig = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.kill(info.pid, sig)
    except ProcessLookupError:
        info.remove()
        return f"{info.name}: already gone (cleaned up its state file)"
    except PermissionError:
        return f"{info.name}: not ours to stop (pid {info.pid})"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not info.is_alive:
            info.remove()  # normally the daemon does this itself; be sure
            return f"{info.name}: stopped"
        time.sleep(0.1)
    return (f"{info.name}: still running after {timeout:g}s "
            f"(pid {info.pid}) — retry with --force")
