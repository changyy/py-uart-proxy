"""
Say *who* has the port when opening it fails because it is in use.

A serial port is exclusive-open — and doubly so for ours, since ``connect``
claims it with ``TIOCEXCL`` (SPEC S15). With background sessions (S17) the
likeliest holder is **our own daemon**, started and forgotten, and the raw error
is ``[Errno 16] Resource busy``: accurate, and no help at all. The fix is almost
always one command away — ``uart-proxy attach``, or a PTY mirror — so that is
what this module works out and says (SPEC S21).

Three pieces, all best-effort and none of them fatal:

* :func:`is_busy_error` — was this open failure "someone else has it"?
* :func:`find_holders` — which processes have the node open (``lsof``).
* :func:`describe_busy` — one line naming the holder and what to do instead,
  preferring a registered background session over a bare pid.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Iterable, Optional

from .daemon import DaemonInfo, list_daemons

#: ``lsof`` walks every open file on the machine; on a busy one that can take a
#: while. A hint that arrives late is worth less than none, so give up quickly.
LSOF_TIMEOUT = 2.0


def is_busy_error(exc: BaseException) -> bool:
    """Whether an open failure means another process holds the port.

    ``uart_helper`` re-raises pyserial's ``SerialException`` as a ``UARTError``,
    so the errno is on the *cause*; walk the chain rather than trusting the
    outermost type. POSIX says ``EBUSY``. Windows says "Access is denied"
    (``ERROR_ACCESS_DENIED``) for a COM port someone else has open, and
    ``uart_helper`` files that under ``UARTPermissionError`` — but a COM port
    has no permission bits to deny, so on Windows it is read as busy.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, "errno", None) == errno.EBUSY:
            return True
        text = str(current).lower()
        if "resource busy" in text or f"errno {errno.EBUSY}]" in text:
            return True
        if sys.platform == "win32" and "access is denied" in text:
            return True
        current = current.__cause__ or current.__context__
    return False


@dataclass(frozen=True)
class Holder:
    pid: int
    command: str


def paired_nodes(path: str) -> list[str]:
    """``path`` plus its macOS dial-in/call-out twin, which interlocks with it.

    Opening ``tty.X`` fails with ``EBUSY`` while ``cu.X`` is held (SPEC S15's
    table), so the holder may be on the node we did *not* ask for.
    """
    nodes = [path]
    head, name = os.path.split(path)
    for a, b in (("tty.", "cu."), ("cu.", "tty.")):
        if head == "/dev" and name.startswith(a):
            nodes.append(os.path.join(head, b + name[len(a):]))
    return nodes


def same_device(a: str, b: str) -> bool:
    """Whether two paths name the same port, counting ``cu.X`` ≡ ``tty.X``."""
    def canon(p: str) -> set[str]:
        return {os.path.realpath(n) for n in paired_nodes(p)}
    return bool(canon(a) & canon(b))


def find_holders(path: str, *, timeout: float = LSOF_TIMEOUT) -> list[Holder]:
    """Processes with ``path`` (or its twin) open, via ``lsof``. [] if unknown.

    There is no portable API for "who has this file open"; ``lsof`` ships with
    macOS and nearly every Linux, and when it is missing, slow or not allowed to
    see another user's processes, the hint simply names nobody.
    """
    lsof = shutil.which("lsof")
    if lsof is None:
        return []
    nodes = [n for n in paired_nodes(path) if os.path.exists(n)]
    if not nodes:
        return []
    try:
        out = subprocess.run(
            [lsof, "-w", "-F", "pc", "--", *nodes],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []

    holders: list[Holder] = []
    pid: Optional[int] = None
    for line in out.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
        elif tag == "c" and pid is not None and pid != os.getpid():
            if all(h.pid != pid for h in holders):
                holders.append(Holder(pid, value))
    return holders


def _holding_daemon(path: str, holders: Iterable[Holder],
                    daemons: Iterable[DaemonInfo]) -> Optional[DaemonInfo]:
    pids = {h.pid for h in holders}
    daemons = list(daemons)
    for info in daemons:
        if info.pid in pids:
            return info
    # No lsof, or it could not see the pid: fall back to what the registry says
    # each daemon was started on.
    for info in daemons:
        if same_device(info.port, path):
            return info
    return None


def describe_busy(
    path: str,
    *,
    holders: Optional[list[Holder]] = None,
    daemons: Optional[list[DaemonInfo]] = None,
) -> str:
    """One line: who has ``path``, and how to get at it without taking it.

    ``holders`` and ``daemons`` default to looking them up; tests pass them in.
    """
    if holders is None:
        holders = find_holders(path)
    if daemons is None:
        try:
            daemons = list_daemons()
        except OSError:
            daemons = []

    info = _holding_daemon(path, holders, daemons)
    if info is not None:
        kind = "session" if info.foreground else "background session"
        where = ", in another terminal" if info.foreground else ""
        hint = (f"port busy: {path} is held by {kind} "
                f"'{info.name}' (pid {info.pid}{where}) — join it with "
                f"'uart-proxy attach {info.name}'")
        if info.proxy_dir:
            hint += f", or open one of its mirrors in {info.proxy_dir}"
        return hint + f"; free it with 'uart-proxy stop {info.name}'"

    if holders:
        who = ", ".join(f"{h.command} (pid {h.pid})" for h in holders)
        return (f"port busy: {path} is held by {who} — close it, or if it is "
                f"uart-proxy, share the port with --proxy-dir (mirrors) or "
                f"--serve and join via 'uart-proxy remote'")

    return (f"port busy: {path} is open in another program — close it, or if it "
            f"is uart-proxy, share the port with --proxy-dir (mirrors) or "
            f"--serve and join via 'uart-proxy remote'")
