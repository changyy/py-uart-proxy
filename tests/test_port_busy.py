"""S21: when the port is busy, say who has it and what to do instead."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import serial
from uart_helper import UARTError, UARTPermissionError, UARTPortNotFoundError

from uart_proxy import cli
from uart_proxy.cli import attach_busy_report
from uart_proxy.core import daemon as daemon_mod
from uart_proxy.core import port_busy as mod
from uart_proxy.core.daemon import DAEMON_SUPPORTED, DaemonInfo
from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.port_busy import (
    Holder,
    describe_busy,
    find_holders,
    is_busy_error,
    paired_nodes,
    same_device,
)
from uart_proxy.core.session import UartSession
from uart_proxy.core.timestamp import TimestampTracker
from uart_proxy.io.uart_source import UartSource

from conftest import FakeSource


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.uart-proxy."""
    home = tmp_path / "home"
    monkeypatch.setenv(daemon_mod.HOME_ENV, str(home))
    return home


def _wrapped(cause: BaseException, outer=UARTError) -> BaseException:
    """What ``uart_helper`` actually raises: its own error, from pyserial's."""
    try:
        try:
            raise cause
        except BaseException as exc:
            raise outer(f"Failed to open /dev/tty.x: {exc}") from exc
    except BaseException as exc:
        return exc


def _daemon(name="usbserial-110", *, pid=None, port="/dev/tty.usbserial-110",
            **kw) -> DaemonInfo:
    return DaemonInfo(name=name, pid=os.getpid() if pid is None else pid,
                      port=port, baud=115200, listen_host="127.0.0.1",
                      listen_port=9600, auth="abc", started_at=time.time(), **kw)


# ── recognising "someone else has it" ───────────────────────────────────────


def test_ebusy_from_pyserial_is_busy_even_when_wrapped():
    cause = serial.SerialException(
        errno.EBUSY, "could not open port /dev/tty.x: [Errno 16] Resource busy")
    assert is_busy_error(_wrapped(cause))


def test_a_bare_oserror_ebusy_is_busy():
    assert is_busy_error(OSError(errno.EBUSY, os.strerror(errno.EBUSY)))


def test_an_absent_device_is_not_busy():
    cause = serial.SerialException(
        errno.ENOENT, "could not open port /dev/tty.x: No such file or directory")
    assert not is_busy_error(_wrapped(cause, UARTPortNotFoundError))


def test_a_permission_problem_is_not_busy_on_posix(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    cause = serial.SerialException(errno.EACCES, "Permission denied")
    assert not is_busy_error(_wrapped(cause, UARTPermissionError))


def test_access_denied_in_another_language_is_still_busy_on_windows(monkeypatch):
    """The text is translated (here Traditional Chinese); the codes are not."""
    monkeypatch.setattr(sys, "platform", "win32")
    cause = serial.SerialException(
        "could not open port 'COM6': PermissionError(13, '存取被拒。', None, 5)")
    assert is_busy_error(_wrapped(cause, UARTPermissionError))
    denied = PermissionError(13, "存取被拒。")
    denied.winerror = 5
    assert is_busy_error(denied)


def test_access_denied_on_windows_means_the_com_port_is_taken(monkeypatch):
    """A COM port has no permission bits; 'Access is denied' is another opener."""
    monkeypatch.setattr(sys, "platform", "win32")
    cause = serial.SerialException(
        "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)")
    assert is_busy_error(_wrapped(cause, UARTPermissionError))


# ── the macOS node pair ─────────────────────────────────────────────────────


def test_tty_and_cu_are_one_port():
    assert paired_nodes("/dev/tty.usbserial-110") == [
        "/dev/tty.usbserial-110", "/dev/cu.usbserial-110"]
    assert same_device("/dev/cu.usbserial-110", "/dev/tty.usbserial-110")
    assert not same_device("/dev/tty.usbserial-110", "/dev/tty.usbserial-120")


def test_other_paths_have_no_twin():
    assert paired_nodes("/dev/ttyUSB0") == ["/dev/ttyUSB0"]
    assert paired_nodes("COM3") == ["COM3"]


# ── the hint ────────────────────────────────────────────────────────────────


def test_our_own_background_session_is_named_with_the_way_in():
    info = _daemon("bench", pid=4242, proxy_dir="/tmp/uart-proxy")
    hint = describe_busy("/dev/tty.usbserial-110",
                         holders=[Holder(4242, "Python")], daemons=[info])
    assert "background session 'bench' (pid 4242)" in hint
    assert "uart-proxy attach bench" in hint
    assert "/tmp/uart-proxy" in hint
    assert "uart-proxy stop bench" in hint


def test_the_registry_answers_when_lsof_cannot():
    """No lsof, or no permission to see the pid: the state file still says
    which daemon was started on this port — counting cu.X as tty.X."""
    info = _daemon("bench", pid=4242, port="/dev/cu.usbserial-110")
    hint = describe_busy("/dev/tty.usbserial-110", holders=[], daemons=[info])
    assert "uart-proxy attach bench" in hint


def test_a_daemon_on_another_port_is_not_blamed():
    info = _daemon("other", pid=4242, port="/dev/tty.usbserial-120")
    hint = describe_busy("/dev/tty.usbserial-110",
                         holders=[Holder(777, "screen")], daemons=[info])
    assert "other" not in hint
    assert "screen (pid 777)" in hint
    assert "--proxy-dir" in hint and "uart-proxy remote" in hint


def test_an_unknown_holder_still_gets_the_alternatives():
    hint = describe_busy("/dev/tty.x", holders=[], daemons=[])
    assert "/dev/tty.x" in hint and "another program" in hint
    assert "--proxy-dir" in hint


def test_defaults_consult_the_real_registry(monkeypatch):
    _daemon("bench", pid=os.getpid(), port="/dev/tty.usbserial-110").write()
    monkeypatch.setattr(mod, "find_holders", lambda path: [])
    assert "uart-proxy attach bench" in describe_busy("/dev/tty.usbserial-110")


# ── finding the holder ──────────────────────────────────────────────────────


def test_no_lsof_means_no_names_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(mod.shutil, "which", lambda name: None)
    path = tmp_path / "node"
    path.write_text("")
    assert find_holders(str(path)) == []


def test_a_missing_node_names_nobody(tmp_path):
    assert find_holders(str(tmp_path / "gone")) == []


@pytest.mark.skipif(mod.shutil.which("lsof") is None, reason="needs lsof")
def test_lsof_names_the_process_that_has_it_open():
    import pty

    master, slave = pty.openpty()
    path = os.ttyname(slave)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sys, time; f = open(sys.argv[1], 'rb'); print('ok', flush=True);"
         " time.sleep(30)", path],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ok"
        pids = {h.pid for h in find_holders(path)}
        assert holder.pid in pids
        assert os.getpid() not in pids, "we are never our own obstacle"
    finally:
        holder.kill()
        holder.wait()
        os.close(master)
        os.close(slave)


# ── UartSource remembers why the open failed ────────────────────────────────


def _source_failing_with(exc) -> UartSource:
    source = UartSource("/dev/tty.fake")

    def fail() -> None:
        raise exc

    source._dev.open = fail
    return source


def test_a_busy_open_is_flagged_and_still_raises():
    source = _source_failing_with(_wrapped(
        serial.SerialException(errno.EBUSY, "[Errno 16] Resource busy")))
    with pytest.raises(UARTError):
        source.open()
    assert source.busy is True


def test_an_absent_device_is_not_flagged_busy():
    source = _source_failing_with(_wrapped(
        serial.SerialException(errno.ENOENT, "No such file or directory"),
        UARTPortNotFoundError))
    with pytest.raises(UARTError):
        source.open()
    assert source.busy is False


# ── the notice ──────────────────────────────────────────────────────────────


def _status(text: str) -> Event:
    return Event(EventKind.STATUS, Direction.SYS, TimestampTracker().stamp(), text=text)


def _reporter(*, busy: bool):
    session = UartSession(FakeSource(), auto_reconnect=False)
    source = SimpleNamespace(busy=busy, device_path="/dev/tty.fake")
    notices: list[str] = []
    session.bus.subscribe(
        lambda e: notices.append(e.text) if e.kind is EventKind.NOTICE else None)
    asked: list[str] = []

    def describe(path: str) -> str:
        asked.append(path)
        return f"port busy: {path}"

    attach_busy_report(session, source, describe=describe)
    return session, source, notices, asked


def test_a_busy_port_is_explained_once_per_streak():
    session, _, notices, asked = _reporter(busy=True)
    for _ in range(5):
        session.bus.publish(_status("waiting"))
    assert notices == ["port busy: /dev/tty.fake"]
    assert asked == ["/dev/tty.fake"], "lsof must not run on every retry"


def test_a_new_streak_after_connecting_is_explained_again():
    session, _, notices, _ = _reporter(busy=True)
    session.bus.publish(_status("waiting"))
    session.bus.publish(_status("connected"))
    session.bus.publish(_status("waiting"))
    assert len(notices) == 2


def test_waiting_for_an_absent_device_says_nothing_extra():
    session, _, notices, asked = _reporter(busy=False)
    session.bus.publish(_status("waiting"))
    assert notices == [] and asked == []


# ── start refuses a port a background session already holds ─────────────────


@pytest.mark.skipif(not DAEMON_SUPPORTED, reason="needs POSIX fork/setsid")
def test_start_on_a_held_port_points_at_the_holder(capsys):
    _daemon("bench", port="/dev/cu.usbserial-110").write()
    code = cli.main(["start", "--port", "/dev/tty.usbserial-110", "--name", "again"])
    err = capsys.readouterr().err
    assert code == 1
    assert "already held by 'bench'" in err
    assert "uart-proxy attach bench" in err
