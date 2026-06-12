"""Shared test fixtures and a fake in-memory transport."""

from __future__ import annotations

import threading

import pytest

from uart_proxy.io.source import DataSource


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

    def feed(self, data: bytes) -> None:
        with self._lock:
            self._buf.extend(data)

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
        with self._lock:
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
