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

import pytest

from uart_proxy.io import uart_source as mod
from uart_proxy.io.uart_source import UartSource, seize_exclusive

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
