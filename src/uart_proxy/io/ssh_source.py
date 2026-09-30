"""
A console over SSH, as the device: ``--port ssh://user@host[:port]`` (SPEC S35).

SSH-based console servers (Opengear, Cisco, Avocent: ``ssh admin:port5@cs``), a
Raspberry Pi with the adapter plugged into it
(``--ssh-command "picocom -b 115200 /dev/ttyUSB0"``), or a BBS such as
``ssh://bbsu@ptt.cc`` — anything reachable by ``ssh -tt``.

This runs the system's **OpenSSH client** in a pty rather than implementing the
protocol: keys, ``known_hosts``, the agent, FIDO keys, ``~/.ssh/config`` and
``ProxyJump`` then behave exactly as they do for ``ssh`` itself, and the part of
this most easily got wrong stays in the hands of the people who maintain it.
Host-key questions and password prompts appear on screen and are answered like
any other input — in character mode, since ssh reads them a key at a time.

Unlike a UART, this transport **can** carry a window size: the pty's size is
passed on by ssh, so the far end draws for the terminal view it is shown in
(or for a fixed ``--term-size``, e.g. 80x24 for a BBS).

POSIX only: it needs a pty.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import shlex
import signal
import struct
import subprocess
import sys
from typing import Optional
from urllib.parse import unquote, urlsplit

from .source import DataSource

logger = logging.getLogger(__name__)

SSH_SUPPORTED = os.name == "posix"

DEFAULT_SIZE = (80, 24)

#: Run in the child between fork and ssh: make the pty its controlling terminal
#: — ssh reads host-key answers and passwords from /dev/tty, which only exists
#: with one — and give it its window size before ssh first looks. A shim rather
#: than preexec_fn, which is unsafe once the parent has threads.
_SHIM = r"""
import fcntl, os, struct, sys, termios
fcntl.ioctl(0, termios.TIOCSCTTY, 0)
cols, rows = int(sys.argv[1]), int(sys.argv[2])
fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
os.execvp(sys.argv[3], sys.argv[3:])
"""


def parse_ssh_url(url: str) -> tuple[Optional[str], str, Optional[int]]:
    """``ssh://user@host:port`` → (user, host, port). Raises ValueError."""
    parts = urlsplit(url)
    if parts.scheme != "ssh":
        raise ValueError(f"not an ssh:// URL: {url!r}")
    if not parts.hostname:
        raise ValueError(f"ssh URL {url!r} needs a host, e.g. ssh://user@host")
    try:
        port = parts.port
    except ValueError:
        raise ValueError(f"ssh URL {url!r} has a bad port") from None
    user = unquote(parts.username) if parts.username else None
    return user, parts.hostname, port


def parse_size(text: str) -> tuple[int, int]:
    """``80x24`` → (80, 24). Raises ValueError."""
    cols, _, rows = text.lower().partition("x")
    size = int(cols), int(rows)
    if not (10 <= size[0] <= 1000 and 2 <= size[1] <= 1000):
        raise ValueError(f"unreasonable terminal size {text!r}")
    return size


class SshSource(DataSource):
    def __init__(
        self,
        url: str,
        *,
        command: Optional[str] = None,
        size: Optional[tuple[int, int]] = None,
        ssh_binary: str = "ssh",
        extra_args: Optional[list[str]] = None,
    ) -> None:
        self._url = url
        self._user, self._host, self._port = parse_ssh_url(url)
        self._command = command
        #: Fixed by --term-size; otherwise it follows the terminal view.
        self.fixed_size = size is not None
        self._size = size or DEFAULT_SIZE
        self._ssh = ssh_binary
        self._extra = list(extra_args or [])
        self._proc: Optional[subprocess.Popen] = None
        self._master: Optional[int] = None
        # Parity with UartSource, for the reporters that ask.
        self.is_exclusive = False
        self.busy = False

    # ── what it runs ────────────────────────────────────────────────────────

    def argv(self) -> list[str]:
        """The ssh command line. ``-tt`` because stdin is not ours to judge."""
        args = [self._ssh, "-tt",
                # A dead connection is noticed (and reconnected) in ~45 s rather
                # than whenever TCP gives up; overridable via ~/.ssh/config.
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=3"]
        if self._port is not None:
            args += ["-p", str(self._port)]
        args += self._extra
        target = f"{self._user}@{self._host}" if self._user else self._host
        args.append(target)
        if self._command:
            args += ["--", *shlex.split(self._command)]
        return args

    @property
    def device_path(self) -> str:
        return self._url

    @property
    def size(self) -> tuple[int, int]:
        return self._size

    # ── lifecycle ───────────────────────────────────────────────────────────

    def open(self) -> None:
        if not SSH_SUPPORTED:
            raise RuntimeError("ssh:// needs a POSIX pty (macOS or Linux)")
        master, slave = os.openpty()
        cols, rows = self._size
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        try:
            self._proc = subprocess.Popen(
                [sys.executable, "-c", _SHIM, str(cols), str(rows), *self.argv()],
                stdin=slave, stdout=slave, stderr=slave,
                start_new_session=True, close_fds=True, env=env,
            )
        except OSError:
            os.close(master)
            raise
        finally:
            os.close(slave)
        self._master = master
        os.set_blocking(master, False)

    def close(self) -> None:
        proc, self._proc = self._proc, None
        master, self._master = self._master, None
        if master is not None:
            os.close(master)  # a hangup for ssh
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
                proc.wait()

    # ── data ────────────────────────────────────────────────────────────────

    def read(self, max_bytes: int, timeout: float) -> bytes:
        master = self._master
        if master is None:
            raise IOError("not connected")
        ready, _, _ = select.select([master], [], [], timeout)
        if not ready:
            self._raise_if_exited()
            return b""
        try:
            data = os.read(master, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as exc:
            if exc.errno == errno.EIO:  # the pty's other end closed: ssh is gone
                self._raise_exited()
            raise
        if not data:
            self._raise_exited()
        return data

    def write(self, data: bytes) -> int:
        master = self._master
        if master is None:
            raise IOError("not connected")
        view = memoryview(data)
        while view:
            try:
                sent = os.write(master, view)
            except BlockingIOError:
                select.select([], [master], [], 1.0)
                continue
            view = view[sent:]
        return len(data)

    def _raise_if_exited(self) -> None:
        if self._proc is not None and self._proc.poll() is not None:
            self._raise_exited()

    def _raise_exited(self) -> None:
        code = self._proc.wait() if self._proc is not None else None
        raise IOError(f"ssh exited (status {code})")

    # ── window size ─────────────────────────────────────────────────────────

    def set_window_size(self, cols: int, rows: int) -> None:
        """Tell the far end our size — ssh forwards the pty's SIGWINCH.

        Ignored with a fixed ``--term-size``: a BBS drawn for 80×24 stays
        80×24 whatever the window does.
        """
        if self.fixed_size or cols < 2 or rows < 2:
            return
        self._size = (cols, rows)
        master = self._master
        if master is None:
            return
        import fcntl
        import termios

        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            logger.debug("could not set the window size", exc_info=True)

    def description(self) -> str:
        cols, rows = self._size
        target = f"{self._user}@{self._host}" if self._user else self._host
        port = f":{self._port}" if self._port else ""
        what = f" · {self._command}" if self._command else ""
        return f"ssh {target}{port}{what} ({cols}×{rows})"
