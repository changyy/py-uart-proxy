"""S15: exclusive claim on the physical port.

What is *not* covered here: that a second ``open()`` of a real serial device
actually fails. ``TIOCEXCL`` is enforced by the tty driver, and the pty driver —
the only tty a test can create — ignores it, so the guarantee is only observable
on real hardware. See SPEC S15 for the manual check. These tests pin down the
part that is testable: that we ask for the claim, on the right fd, and that
failing to get one never breaks the session.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from uart_proxy.cli import attach_exclusivity_report
from uart_proxy.core.events import Direction, Event, EventKind
from uart_proxy.core.session import UartSession
from uart_proxy.core.timestamp import TimestampTracker
from uart_proxy.io import uart_source as mod
from uart_proxy.io.uart_source import UartSource, seize_exclusive

from conftest import FakeSource

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX ioctl behaviour")


def test_claim_succeeds_on_a_tty():
    import pty

    master, slave = pty.openpty()
    try:
        assert seize_exclusive(slave) is True
    finally:
        os.close(master)
        os.close(slave)


def test_claim_on_a_non_tty_is_reported_not_raised():
    """A pipe (or a closed fd) has nothing to claim — best-effort, never fatal."""
    r, w = os.pipe()
    try:
        assert seize_exclusive(r) is False
    finally:
        os.close(r)
        os.close(w)


# ── UartSource wiring ───────────────────────────────────────────────────────


class FakePort:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


class FakeDevice:
    """Stands in for uart_helper.UARTDevice, exposing the same private attr."""

    def __init__(self, identity, config, *, fd: int = -1, expose: bool = True) -> None:
        self.identity = identity
        self.config = config
        self._serial = FakePort(fd) if expose else None
        self.opened = False

    def open(self) -> None:
        self.opened = True

    def close(self) -> None:
        self.opened = False


@pytest.fixture
def pty_fd():
    import pty

    master, slave = pty.openpty()
    yield slave
    for fd in (master, slave):
        try:
            os.close(fd)
        except OSError:
            pass


def _patch_device(monkeypatch, pty_fd, *, expose: bool = True):
    seen: dict = {}

    def factory(identity, config):
        dev = FakeDevice(identity, config, fd=pty_fd, expose=expose)
        seen["dev"] = dev
        return dev

    monkeypatch.setattr(mod, "UARTDevice", factory)
    calls: list[int] = []
    monkeypatch.setattr(mod, "seize_exclusive", lambda fd: (calls.append(fd), True)[1])
    return seen, calls


def test_open_claims_the_port_by_default(monkeypatch, pty_fd):
    _, calls = _patch_device(monkeypatch, pty_fd)
    source = UartSource("/dev/cu.fake")
    source.open()
    assert calls == [pty_fd], "the claim must be made on the open port's own fd"
    assert source.is_exclusive is True


def test_no_exclusive_leaves_the_port_shareable(monkeypatch, pty_fd):
    _, calls = _patch_device(monkeypatch, pty_fd)
    source = UartSource("/dev/cu.fake", exclusive=False)
    source.open()
    assert calls == []
    assert source.is_exclusive is False


def test_close_clears_the_claim(monkeypatch, pty_fd):
    _patch_device(monkeypatch, pty_fd)
    source = UartSource("/dev/cu.fake")
    source.open()
    source.close()
    assert source.is_exclusive is False


def test_an_unreachable_fd_does_not_break_open(monkeypatch, pty_fd):
    """If uart_helper ever renames its private port attribute we lose the
    claim — we must not lose the session with it."""
    _, calls = _patch_device(monkeypatch, pty_fd, expose=False)
    source = UartSource("/dev/cu.fake")
    source.open()  # must not raise
    assert calls == []
    assert source.is_exclusive is False


# ── the claim is reported, so a silent failure can't be mistaken for safety ──


def _connected(text: str = "connected") -> Event:
    return Event(EventKind.STATUS, Direction.SYS, TimestampTracker().stamp(), text=text)


def _reporter(*, is_exclusive: bool, requested: bool = True):
    """A session with the reporter attached; returns (session, notices)."""
    session = UartSession(FakeSource(), auto_reconnect=False)
    source = SimpleNamespace(is_exclusive=is_exclusive, device_path="/dev/tty.fake")
    notices: list[str] = []
    session.bus.subscribe(
        lambda e: notices.append(e.text) if e.kind is EventKind.NOTICE else None
    )
    attach_exclusivity_report(session, source, requested=requested)
    return session, source, notices


def test_a_successful_claim_is_announced():
    session, _, notices = _reporter(is_exclusive=True)
    session.bus.publish(_connected())
    assert len(notices) == 1
    assert "claimed /dev/tty.fake" in notices[0] and "TIOCEXCL" in notices[0]


def test_a_failed_claim_is_announced_loudly():
    """The claim is best-effort; failing silently would leave the operator
    believing the wire is protected when it isn't."""
    session, _, notices = _reporter(is_exclusive=False, requested=True)
    session.bus.publish(_connected())
    assert len(notices) == 1
    assert "COULD NOT claim" in notices[0] and "split the byte stream" in notices[0]


def test_opting_out_is_reported_as_a_choice_not_a_failure():
    session, _, notices = _reporter(is_exclusive=False, requested=False)
    session.bus.publish(_connected())
    assert len(notices) == 1
    assert "--no-exclusive" in notices[0] and "COULD NOT" not in notices[0]


def test_nothing_is_said_before_the_port_opens():
    session, _, notices = _reporter(is_exclusive=True)
    for state in ("waiting", "reconnecting", "error"):
        session.bus.publish(_connected(state))
    assert notices == []


def test_a_flapping_device_does_not_repeat_the_same_line():
    session, source, notices = _reporter(is_exclusive=True)
    for _ in range(5):
        session.bus.publish(_connected())
    assert len(notices) == 1, "reconnects must not fill the log with one line"

    # …but a genuine change of state is worth saying.
    source.is_exclusive = False
    session.bus.publish(_connected())
    assert len(notices) == 2 and "COULD NOT claim" in notices[1]
