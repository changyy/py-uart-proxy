"""Shared test fixtures and a fake in-memory transport."""

from __future__ import annotations

import signal
import subprocess
import threading

import pytest

from uart_proxy.io.source import DataSource


@pytest.fixture(autouse=True)
def no_real_hardware(request, monkeypatch):
    """In-process tests never see this machine's serial ports or detach a daemon.

    A test that scans for a port and finds a real one goes on to open it — and
    a developer's plugged-in adapter matching a test profile once did exactly
    that, detaching a daemon onto it. Tests that need either say so with
    ``@pytest.mark.real_ports`` / ``@pytest.mark.real_daemonize``; subprocess
    tests are unaffected and use ptys.
    """
    from uart_proxy import cli

    if "real_ports" not in request.keywords:
        from uart_proxy.io import uart_source

        monkeypatch.setattr(cli, "_scan_ports", lambda: [])
        # UartSource scans too, to follow an adapter that re-enumerates.
        monkeypatch.setattr(uart_source, "_scan_ports", lambda: [])
    if "real_daemonize" not in request.keywords:
        def refuse(**kwargs):
            raise AssertionError("an in-process test tried to detach a daemon")

        monkeypatch.setattr(cli, "daemonize", refuse)


class FakeSource(DataSource):
    """
    An in-memory DataSource for tests.

    ``feed(data)`` queues bytes that ``read`` will return. ``writes`` records
    everything written. If ``echo`` is True, writes are looped back to reads.
    """

    def __init__(
        self,
        *,
        echo: bool = False,
        writable: bool = True,
        fail_opens: int = 0,
    ) -> None:
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._echo = echo
        self._writable = writable
        self.fail_opens = fail_opens  # first N open() calls raise (device absent)
        self.writes: list[bytes] = []
        self.open_calls = 0
        self.opened = False
        self.closed = False
        self._drop = threading.Event()  # set -> next read() raises (device drop)
        # Signals feed() -> read(). A Condition rather than an Event because an
        # Event has a lost-wakeup window: data fed between wait() returning and
        # clear() would be missed until the next poll.
        self._arrived = threading.Condition(self._lock)

    def feed(self, data: bytes) -> None:
        with self._arrived:
            self._buf.extend(data)
            self._arrived.notify_all()

    def drop(self) -> None:
        """Simulate the device going away on the next read()."""
        self._drop.set()

    def open(self) -> None:
        self.open_calls += 1
        if self.open_calls <= self.fail_opens:
            raise IOError("device not present")
        self.opened = True
        self.closed = False

    def close(self) -> None:
        self.closed = True
        self.opened = False

    def read(self, max_bytes: int, timeout: float) -> bytes:
        if self._drop.is_set():
            self._drop.clear()
            raise IOError("device disconnected")
        # Wait out the timeout like a real source would if there's nothing yet.
        # Returning b"" immediately turns the session's read loop into a busy
        # spin, which starves the asyncio loop the TUI tests run on.
        with self._arrived:
            if not self._buf:
                self._arrived.wait(timeout)
            if not self._buf:
                return b""
            out = bytes(self._buf[:max_bytes])
            del self._buf[: len(out)]
            return out

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        if self._echo:
            self.feed(data)
        return len(data)

    def description(self) -> str:
        return "fake-source"

    @property
    def writable(self) -> bool:
        return self._writable


@pytest.fixture
def fake_source() -> FakeSource:
    return FakeSource()


def wait_or_dump(proc, timeout):
    """proc's exit status — or, when it does not exit in time, fail with every
    thread's stack (it runs with PYTHONFAULTHANDLER; SIGABRT dumps them)."""
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.send_signal(signal.SIGABRT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        err = proc.stderr.read()
        err = err.decode("utf-8", "replace") if isinstance(err, bytes) else err
        pytest.fail(f"did not exit within {timeout}s; its threads:\n{err[-8000:]}")
