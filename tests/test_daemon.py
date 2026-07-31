"""S17: background sessions — start detached, list, stop."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from uart_proxy.core import daemon as mod
from uart_proxy.core.daemon import (
    DAEMON_SUPPORTED,
    DaemonInfo,
    DaemonNotFound,
    daemon_dir,
    find_daemon,
    list_daemons,
    new_auth_code,
    prune_dead,
)


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.uart-proxy."""
    home = tmp_path / "home"
    monkeypatch.setenv(mod.HOME_ENV, str(home))
    return home


def _info(name="usbserial-110", *, pid=None, **kw) -> DaemonInfo:
    return DaemonInfo(
        name=name,
        pid=os.getpid() if pid is None else pid,
        port=kw.pop("port", "/dev/tty.usbserial-110"),
        baud=kw.pop("baud", 115200),
        listen_host=kw.pop("listen_host", "127.0.0.1"),
        listen_port=kw.pop("listen_port", 9600),
        auth=kw.pop("auth", "abc123"),
        started_at=kw.pop("started_at", time.time()),
        **kw,
    )


# ── the state file is the registry ──────────────────────────────────────────


def test_state_survives_a_round_trip():
    original = _info(log_dir="/tmp/logs", proxy_dir="/tmp/mirrors")
    original.write()
    loaded = mod.read_state(original.path)
    assert loaded == original


def test_the_state_file_is_private_because_it_holds_the_auth_code():
    info = _info()
    info.write()
    assert oct(os.stat(info.path).st_mode)[-3:] == "600"
    assert oct(os.stat(daemon_dir()).st_mode)[-3:] == "700"
    assert info.auth in open(info.path, encoding="utf-8").read()


def test_a_file_from_another_version_still_loads():
    """Forward/backward compatibility: unknown keys are ignored rather than
    making a running daemon unlistable."""
    info = _info()
    info.write()
    data = json.load(open(info.path, encoding="utf-8"))
    data["something_from_the_future"] = True
    json.dump(data, open(info.path, "w", encoding="utf-8"))
    assert mod.read_state(info.path).name == info.name


def test_unreadable_state_is_skipped_not_fatal():
    os.makedirs(daemon_dir(), exist_ok=True)
    with open(os.path.join(daemon_dir(), "broken.json"), "w") as fh:
        fh.write("{ this is not json")
    assert list_daemons(include_dead=True) == []


def test_a_generated_auth_code_is_not_guessable():
    codes = {new_auth_code() for _ in range(50)}
    assert len(codes) == 50
    assert all(len(c) >= 16 for c in codes)


# ── liveness ────────────────────────────────────────────────────────────────


def test_a_live_pid_is_reported_running():
    assert _info().is_alive is True


def test_a_dead_pid_is_not():
    # PID 1 exists, so pick something implausible instead: our own child, reaped.
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    assert _info(pid=proc.pid).is_alive is False


def test_dead_sessions_are_pruned_and_hidden():
    """A daemon killed with SIGKILL can't clean up after itself (S16), so
    whichever command looks next has to."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    alive, dead = _info("alive"), _info("dead", pid=proc.pid)
    alive.write()
    dead.write()

    assert {d.name for d in list_daemons()} == {"alive"}
    assert {d.name for d in list_daemons(include_dead=True)} == {"alive", "dead"}
    assert [d.name for d in prune_dead()] == ["dead"]
    assert os.path.exists(alive.path) and not os.path.exists(dead.path)


def test_last_activity_comes_from_the_log_files(tmp_path):
    """The daemon's counters are in its own memory; the log's mtime is on disk
    and answers what `status` is really asked: is it still hearing anything?"""
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "output.log").write_bytes(b"hello")
    info = _info(log_dir=str(log_dir))
    assert abs(info.last_activity() - time.time()) < 5
    assert _info().last_activity() is None  # not recording


# ── name resolution ─────────────────────────────────────────────────────────


def test_a_lone_session_needs_no_name():
    _info("only-one").write()
    assert find_daemon().name == "only-one"


def test_several_sessions_force_you_to_say_which():
    _info("one").write()
    _info("two").write()
    with pytest.raises(DaemonNotFound) as exc:
        find_daemon()
    assert "one" in str(exc.value) and "two" in str(exc.value)


def test_an_unknown_name_lists_what_does_exist():
    _info("real").write()
    with pytest.raises(DaemonNotFound) as exc:
        find_daemon("imaginary")
    assert "real" in str(exc.value)


def test_no_sessions_at_all_suggests_starting_one():
    with pytest.raises(DaemonNotFound) as exc:
        find_daemon()
    assert "start" in str(exc.value)


# ── end to end ──────────────────────────────────────────────────────────────


def _cli(*args, home, extra_env=None):
    env = dict(os.environ, **{mod.HOME_ENV: str(home)}, **(extra_env or {}))
    return subprocess.run(
        [sys.executable, "-m", "uart_proxy", *args],
        env=env, capture_output=True, text=True, timeout=60,
    )


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="needs POSIX fork/setsid")
def test_start_status_stop(tmp_path, isolated_home):
    """The whole point, against a PTY standing in for the adapter: the session
    outlives the command that launched it, and records while detached."""
    import pty
    import tty

    master, slave = pty.openpty()
    tty.setraw(master)
    tty.setraw(slave)
    device = os.ttyname(slave)
    logs = tmp_path / "logs"
    mirrors = tmp_path / "mirrors"

    started = _cli("start", "--port", device, "--name", "probe",
                   "--output-dir", str(logs), "--proxy-dir", str(mirrors),
                   "--listen-port", "0", home=isolated_home)
    os.close(slave)
    try:
        assert started.returncode == 0, started.stderr
        info = find_daemon("probe")
        assert info.is_alive

        # It is a *separate* process: the launching command has already exited.
        assert info.pid != os.getpid()

        # It keeps recording with nobody attached.
        os.write(master, b"while you were out\r\n")
        deadline = time.monotonic() + 10
        raw = logs / "output.log"
        while time.monotonic() < deadline:
            if raw.exists() and b"while you were out" in raw.read_bytes():
                break
            time.sleep(0.1)
        assert b"while you were out" in raw.read_bytes()

        # …and the mirrors are the daemon's, so they are up too.
        assert sorted(os.listdir(mirrors)) == ["probe-0", "probe-1"]

        listed = _cli("status", "--json", home=isolated_home)
        data = json.loads(listed.stdout)["data"]
        assert [d["name"] for d in data] == ["probe"]
        # --listen-port 0 lets the kernel choose; the state file must record the
        # port actually bound, or nothing could find the daemon afterwards.
        host, _, port = data[0]["listen"].partition(":")
        assert host == "127.0.0.1" and int(port) > 0

        stopped = _cli("stop", "probe", home=isolated_home)
        assert stopped.returncode == 0 and "stopped" in stopped.stdout
        assert list_daemons() == []
        assert os.listdir(mirrors) == [], "an ordered stop clears the symlinks"
    finally:
        for info in list_daemons():
            try:
                os.kill(info.pid, signal.SIGKILL)
            except OSError:
                pass
        os.close(master)


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="needs POSIX fork/setsid")
def test_a_second_start_on_the_same_name_is_refused(tmp_path, isolated_home):
    import pty

    master, slave = pty.openpty()
    device = os.ttyname(slave)
    first = _cli("start", "--port", device, "--name", "dup",
                 "--output-dir", str(tmp_path / "l1"), "--listen-port", "0",
                 home=isolated_home)
    os.close(slave)
    try:
        assert first.returncode == 0, first.stderr
        again = _cli("start", "--port", device, "--name", "dup",
                     "--output-dir", str(tmp_path / "l2"), "--listen-port", "0",
                     home=isolated_home)
        assert again.returncode == 1
        assert "already running" in again.stderr
    finally:
        _cli("stop", "--all", home=isolated_home)
        for info in list_daemons():
            try:
                os.kill(info.pid, signal.SIGKILL)
            except OSError:
                pass
        os.close(master)


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="needs POSIX fork/setsid")
def test_a_daemon_that_cannot_start_fails_the_command(tmp_path, isolated_home):
    """`start` must not report success and leave a corpse: the child reports back
    over a pipe before the parent exits."""
    import pty

    master, slave = pty.openpty()
    device = os.ttyname(slave)
    blocker = tmp_path / "in-the-way"
    blocker.write_text("precious")
    result = _cli("start", "--port", device, "--proxy", str(blocker),
                  "--output-dir", str(tmp_path / "l"), home=isolated_home)
    os.close(slave)
    os.close(master)
    assert result.returncode == 1
    assert "Could not start" in result.stderr
    assert "not a symlink" in result.stderr
    assert list_daemons(include_dead=True) == [], "no state file left behind"
    assert blocker.read_text() == "precious"


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="needs POSIX fork/setsid")
def test_an_absent_device_is_not_a_startup_failure(tmp_path, isolated_home):
    """Waiting for a device to be plugged in is the documented behaviour (S12),
    so it must not be reported as a failure to launch."""
    result = _cli("start", "--port", "/dev/cu.nothing-here", "--name", "ghost",
                  "--output-dir", str(tmp_path / "l"), "--listen-port", "0",
                  home=isolated_home)
    try:
        assert result.returncode == 0, result.stderr
        assert find_daemon("ghost").is_alive
    finally:
        _cli("stop", "--all", home=isolated_home)


def test_status_says_something_useful_when_nothing_runs(isolated_home):
    result = _cli("status", home=isolated_home)
    assert result.returncode == 0
    assert "No background session" in result.stdout
    assert "uart-proxy start" in result.stdout


def test_stop_with_no_session_explains_rather_than_crashing(isolated_home):
    result = _cli("stop", home=isolated_home)
    assert result.returncode == 1
    assert "no session is running" in result.stderr
