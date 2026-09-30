"""S35: a console over SSH as the device — driven against a fake `ssh`."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

from uart_proxy import cli
from uart_proxy.core.events import Direction, EventKind
from uart_proxy.core.pty_proxy import device_stem
from uart_proxy.core.session import UartSession
from uart_proxy.io.ssh_source import (
    SSH_SUPPORTED,
    SshSource,
    parse_size,
    parse_ssh_url,
)
from uart_proxy.io.url_source import check_port_url

pytestmark = pytest.mark.skipif(not SSH_SUPPORTED, reason="needs a POSIX pty")

FAKE_SSH = textwrap.dedent('''\
    import fcntl, os, signal, struct, sys, termios

    log = os.environ.get("FAKE_SSH_LOG")
    if log:
        with open(log, "a") as fh:
            fh.write(" ".join(sys.argv[1:]) + "\\n")
    if os.environ.get("FAKE_SSH_EXIT"):
        sys.stdout.write("Connection closed.\\r\\n")
        sys.stdout.flush()
        sys.exit(255)

    def size():
        rows, cols, _, _ = struct.unpack("HHHH", fcntl.ioctl(
            0, termios.TIOCGWINSZ, b"\\0" * 8))
        return f"{cols}x{rows}"

    def on_winch(signum, frame):
        sys.stdout.write(f"resized={size()}\\r\\n")
        sys.stdout.flush()

    signal.signal(signal.SIGWINCH, on_winch)
    try:
        os.close(os.open("/dev/tty", os.O_RDWR))
        ctty = "yes"
    except OSError:
        ctty = "no"
    sys.stdout.write(f"Welcome size={size()} ctty={ctty}\\r\\n")
    sys.stdout.flush()
    for line in sys.stdin:
        line = line.strip()
        if line == "exit":
            break
        sys.stdout.write(f"echo:{line}\\r\\n")
        sys.stdout.flush()
''')


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """An executable called `ssh`, first on PATH, that logs how it was run."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "ssh"
    script.write_text(f"#!{sys.executable}\n" + FAKE_SSH)
    script.chmod(0o755)
    log = tmp_path / "ssh-calls.log"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_SSH_LOG", str(log))
    return SimpleCalls(log)


class SimpleCalls:
    def __init__(self, log) -> None:
        self.log = log

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


def _wait_for(predicate, timeout=8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _read_until(source, needle: bytes, timeout=8.0) -> bytes:
    got = b""
    deadline = time.monotonic() + timeout
    while needle not in got and time.monotonic() < deadline:
        got += source.read(4096, 0.1)
    return got


# ── URLs and the command line ───────────────────────────────────────────────


def test_urls_parse():
    assert parse_ssh_url("ssh://bbsu@ptt.cc") == ("bbsu", "ptt.cc", None)
    assert parse_ssh_url("ssh://admin%3Aport5@cs:2222") == ("admin:port5", "cs", 2222)
    assert parse_ssh_url("ssh://lab") == (None, "lab", None)
    with pytest.raises(ValueError):
        parse_ssh_url("ssh://")


def test_ssh_urls_pass_the_port_check_without_a_port():
    assert check_port_url("ssh://bbsu@ptt.cc") is None
    assert "needs a host" in check_port_url("ssh://")


def test_the_ssh_command_line():
    source = SshSource("ssh://admin@cs:2222", command="picocom -b 115200 /dev/ttyUSB0",
                       extra_args=["-o", "StrictHostKeyChecking=accept-new"])
    argv = source.argv()
    assert argv[:2] == ["ssh", "-tt"], "-tt: a tty even though ours is a pty"
    assert "ServerAliveInterval=15" in argv
    assert argv[argv.index("-p") + 1] == "2222"
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[argv.index("admin@cs") + 1:] == ["--", "picocom", "-b", "115200",
                                                  "/dev/ttyUSB0"]


def test_no_user_and_no_port_leave_them_to_ssh_config():
    argv = SshSource("ssh://lab").argv()
    assert "-p" not in argv and argv[-1] == "lab"


@pytest.mark.parametrize("text, size", [("80x24", (80, 24)), ("132X50", (132, 50))])
def test_sizes_parse(text, size):
    assert parse_size(text) == size


@pytest.mark.parametrize("bad", ["80", "x24", "1x1", "99999x24", "axb"])
def test_bad_sizes_are_refused(bad):
    with pytest.raises(ValueError):
        parse_size(bad)


def test_the_stem_names_the_host():
    assert device_stem("ssh://bbsu@ptt.cc") == "ssh-ptt.cc"
    assert device_stem("ssh://admin@cs:2222") == "ssh-cs-2222"


def test_the_description_says_where_and_how_big():
    source = SshSource("ssh://bbsu@ptt.cc", size=(80, 24))
    assert source.description() == "ssh bbsu@ptt.cc (80×24)"


# ── against the fake ssh ────────────────────────────────────────────────────


def test_it_runs_ssh_in_a_pty_with_a_controlling_terminal(fake_ssh):
    """Without a controlling tty ssh could not ask about a host key or for a
    password: it reads those from /dev/tty."""
    source = SshSource("ssh://bbsu@ptt.cc")
    source.open()
    try:
        banner = _read_until(source, b"ctty=")
        assert b"Welcome size=80x24" in banner, banner
        assert b"ctty=yes" in _read_until(source, b"\n") or b"ctty=yes" in banner
        assert fake_ssh.calls()[0].startswith("-tt ")
        assert fake_ssh.calls()[0].endswith("bbsu@ptt.cc")
    finally:
        source.close()


def test_what_is_typed_reaches_the_far_end(fake_ssh):
    source = SshSource("ssh://lab")
    source.open()
    try:
        _read_until(source, b"ctty=")
        source.write(b"hello\r")
        assert b"echo:hello" in _read_until(source, b"echo:hello")
    finally:
        source.close()


def test_the_window_size_is_passed_on_and_follows_resizes(fake_ssh):
    source = SshSource("ssh://lab", size=None)
    source.open()
    try:
        _read_until(source, b"ctty=")
        source.set_window_size(132, 40)
        assert b"resized=132x40" in _read_until(source, b"resized=")
        assert source.size == (132, 40)
    finally:
        source.close()


def test_a_fixed_size_stays_fixed(fake_ssh):
    source = SshSource("ssh://bbsu@ptt.cc", size=(80, 24))
    source.open()
    try:
        assert b"size=80x24" in _read_until(source, b"ctty=")
        source.set_window_size(200, 60)
        got = _read_until(source, b"resized=", timeout=0.8)
        assert b"resized=" not in got and source.size == (80, 24)
    finally:
        source.close()


def test_an_initial_size_is_there_before_ssh_first_looks(fake_ssh):
    source = SshSource("ssh://lab", size=(100, 30))
    source.open()
    try:
        assert b"Welcome size=100x30" in _read_until(source, b"ctty=")
    finally:
        source.close()


def test_ssh_exiting_is_a_dropped_connection(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_EXIT", "1")
    source = SshSource("ssh://lab")
    source.open()
    try:
        with pytest.raises(IOError, match=r"ssh exited \(status 255\)"):
            for _ in range(100):
                source.read(4096, 0.1)
    finally:
        source.close()


def test_a_session_reconnects_by_running_ssh_again(fake_ssh, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_EXIT", "1")
    session = UartSession(SshSource("ssh://lab"), reconnect_interval=0.05)
    session.start()
    try:
        assert _wait_for(lambda: len(fake_ssh.calls()) >= 3), "ran ssh again, and again"
    finally:
        session.stop()


def test_close_leaves_no_ssh_behind(fake_ssh):
    source = SshSource("ssh://lab")
    source.open()
    pid = source._proc.pid
    _read_until(source, b"ctty=")
    source.close()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# ── the CLI ─────────────────────────────────────────────────────────────────


def _connect_args(*extra):
    return cli.build_parser().parse_args(["connect", "--port", "ssh://bbsu@ptt.cc",
                                          "--no-log", *extra])


def test_connect_builds_an_ssh_source_and_defaults_to_char_mode(monkeypatch):
    seen = {}

    def fake_run(session, args, **kw):
        seen["source"], seen["input"] = session.source, args.input
        return 0

    monkeypatch.setattr(cli, "_run_session", fake_run)
    assert cli.cmd_connect(_connect_args("--term-size", "80x24",
                                         "--ssh-option", "BatchMode=yes")) == 0
    source = seen["source"]
    assert isinstance(source, SshSource)
    assert source.size == (80, 24) and source.fixed_size
    assert "BatchMode=yes" in source.argv()
    assert seen["input"] == "char", "a shell or BBS reads keys, not lines"


def test_an_explicit_input_mode_is_respected(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_run_session",
                        lambda session, args, **kw: seen.setdefault("input", args.input) and 0)
    cli.cmd_connect(_connect_args("--input", "line"))
    assert seen["input"] == "line"


def test_a_bad_term_size_is_an_error(capsys):
    assert cli.cmd_connect(_connect_args("--term-size", "huge")) == 1
    assert "--term-size" in capsys.readouterr().err


def test_serial_ports_still_default_to_line_mode(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_run_session",
                        lambda session, args, **kw: seen.setdefault("input", args.input) or 0)
    args = cli.build_parser().parse_args(["connect", "--port", "/dev/tty.x", "--no-log"])
    cli.cmd_connect(args)
    assert seen["input"] is None, "None, which _run_session reads as line"


# ── the TUI tells ssh its size ──────────────────────────────────────────────


def test_the_terminal_view_size_reaches_the_transport():
    from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE, UartProxyApp

    if not _TEXTUAL_AVAILABLE:
        pytest.skip("textual")
    from conftest import FakeSource

    class SizedSource(FakeSource):
        def __init__(self):
            super().__init__()
            self.sizes = []

        def set_window_size(self, cols, rows):
            self.sizes.append((cols, rows))

    async def scenario():
        source = SizedSource()
        session = UartSession(source, auto_reconnect=False)
        app = UartProxyApp(session, input_mode="char")
        async with app.run_test(size=(120, 40)) as pilot:
            for _ in range(5):
                await asyncio.sleep(0.03)
                await pilot.pause()
            await pilot.resize_terminal(100, 30)
            for _ in range(5):
                await asyncio.sleep(0.03)
                await pilot.pause()
        session.stop()
        return source.sizes, app

    sizes, _ = asyncio.run(scenario())
    assert sizes, "the transport was never told a size"
    assert sizes[0][0] == 120
    assert sizes[-1][0] == 100, "and told again after a resize"


# ── end to end ──────────────────────────────────────────────────────────────


def test_connect_over_ssh_end_to_end(fake_ssh, tmp_path):
    logs = tmp_path / "logs"
    env = dict(os.environ, HOME=str(tmp_path))
    proc = subprocess.Popen(
        [sys.executable, "-m", "uart_proxy", "connect", "--port", "ssh://bbsu@ptt.cc",
         "--no-tui", "--output-dir", str(logs), "--term-size", "80x24"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env)
    try:
        raw = logs / "output.log"
        assert _wait_for(lambda: raw.exists() and b"Welcome" in raw.read_bytes(), 15)
        assert b"size=80x24" in raw.read_bytes()
    finally:
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=10)
    assert "OpenSSH" in out.decode(), "the network note names the transport"
    header = (logs / "output-timestamp.log").read_text().splitlines()[0]
    assert "ssh bbsu@ptt.cc (80×24)" in header


# ── auto size: every row the view can spare, and the real terminal headless ──


class _SizedSource:
    """Just enough of a source to be told sizes."""

    def __init__(self, fixed=False):
        self.sizes = []
        self.fixed_size = fixed

    def set_window_size(self, cols, rows):
        if not self.fixed_size:
            self.sizes.append((cols, rows))


def _tui_sizes(input_mode, toggle=False, size=(120, 40)):
    from uart_proxy.ui.tui import _TEXTUAL_AVAILABLE, UartProxyApp

    if not _TEXTUAL_AVAILABLE:
        pytest.skip("textual")
    from conftest import FakeSource

    class SizedFake(FakeSource):
        def __init__(self):
            super().__init__()
            self.sizes = []

        def set_window_size(self, cols, rows):
            self.sizes.append((cols, rows))

    async def scenario():
        source = SizedFake()
        session = UartSession(source, auto_reconnect=False)
        app = UartProxyApp(session, input_mode=input_mode)
        async with app.run_test(size=size) as pilot:
            for _ in range(6):
                await asyncio.sleep(0.03)
                await pilot.pause()
            shown = app.query_one("#cmd").display
            if toggle:
                await pilot.press("ctrl+right_square_bracket")
                await pilot.press("c")
                for _ in range(6):
                    await asyncio.sleep(0.03)
                    await pilot.pause()
            shown_after = app.query_one("#cmd").display
        session.stop()
        return source.sizes, shown, shown_after

    return asyncio.run(scenario())


def test_character_mode_gives_the_input_rows_to_the_screen():
    """Compared, not hard-coded: the rows the box took are Textual's layout."""
    char_sizes, char_shown, _ = _tui_sizes("char")
    line_sizes, line_shown, _ = _tui_sizes("line")
    assert (char_shown, line_shown) == (False, True), "no input box in character mode"
    assert char_sizes[-1][0] == line_sizes[-1][0] == 120
    assert char_sizes[-1][1] > line_sizes[-1][1], (char_sizes, line_sizes)
    assert line_sizes[-1][1] >= 40 - 6, "only the bars are taken off, not more"


def test_switching_to_character_mode_hands_over_the_rows_and_says_so():
    sizes, shown, shown_after = _tui_sizes("line", toggle=True)
    assert (shown, shown_after) == (True, False)
    assert sizes[-1][1] > sizes[0][1], f"the far end is told the screen grew: {sizes}"


def test_term_size_auto_is_the_default_said_out_loud(monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "_run_session",
                        lambda session, args, **kw: seen.setdefault("s", session.source) and 0)
    cli.cmd_connect(_connect_args("--term-size", "auto"))
    assert seen["s"].fixed_size is False


def _headless_follow(monkeypatch, *, tty=True, fixed=False, sizes=((150, 45),)):
    from uart_proxy.ui import headless

    queue = list(sizes)
    monkeypatch.setattr(headless.sys.stdout, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(headless.sys.stdout, "fileno", lambda: 1, raising=False)
    monkeypatch.setattr(headless.os, "get_terminal_size",
                        lambda fd: os.terminal_size(queue[0] if len(queue) == 1
                                                    else queue.pop(0)))
    source = _SizedSource(fixed=fixed)
    session = SimpleSession(source)
    restore = headless.follow_terminal_size(session)
    return source, restore


class SimpleSession:
    def __init__(self, source):
        self.source = source


def test_headless_tells_ssh_the_real_terminal_size(monkeypatch):
    source, restore = _headless_follow(monkeypatch)
    try:
        assert source.sizes == [(150, 45)]
    finally:
        if restore:
            restore()


def test_headless_follows_the_terminal_when_it_is_resized(monkeypatch):
    source, restore = _headless_follow(monkeypatch, sizes=((150, 45), (90, 30)))
    try:
        assert restore is not None, "a SIGWINCH handler is installed"
        os.kill(os.getpid(), signal.SIGWINCH)
        assert _wait_for(lambda: source.sizes[-1] == (90, 30), timeout=2)
    finally:
        restore()


def test_headless_restores_the_previous_winch_handler(monkeypatch):
    before = signal.getsignal(signal.SIGWINCH)
    _, restore = _headless_follow(monkeypatch)
    assert signal.getsignal(signal.SIGWINCH) is not before
    restore()
    assert signal.getsignal(signal.SIGWINCH) == before


def test_no_terminal_no_size_a_daemon_keeps_its_own(monkeypatch):
    source, restore = _headless_follow(monkeypatch, tty=False)
    assert source.sizes == [] and restore is None


def test_a_fixed_size_is_not_overridden_headless_either(monkeypatch):
    source, restore = _headless_follow(monkeypatch, fixed=True)
    try:
        assert source.sizes == []
    finally:
        if restore:
            restore()
