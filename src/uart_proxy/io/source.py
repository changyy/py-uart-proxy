"""
The transport abstraction that a session drives.

Both a local UART port and a remote socket connection look the same to the
session: open it, read byte chunks, write byte chunks, close it. This keeps the
entire pipeline (timestamps, recorder, plugins, proxy) identical regardless of
where the bytes physically come from.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class DataSource(ABC):
    """A bidirectional byte transport."""

    @abstractmethod
    def open(self) -> None:
        """Open the transport. Raises on failure."""

    @abstractmethod
    def close(self) -> None:
        """Close the transport. Safe to call more than once."""

    @abstractmethod
    def read(self, max_bytes: int, timeout: float) -> bytes:
        """
        Read up to ``max_bytes``, waiting at most ``timeout`` seconds.

        Returns ``b""`` if nothing arrived within the timeout. Raises on a
        fatal transport error (e.g. the device was unplugged or the peer
        closed the connection) to signal the session to stop.
        """

    @abstractmethod
    def write(self, data: bytes) -> int:
        """Write ``data`` and return the number of bytes written."""

    @abstractmethod
    def description(self) -> str:
        """A short human-readable description for status display."""

    @property
    def writable(self) -> bool:
        """Whether this source accepts writes (e.g. readonly remote = False)."""
        return True
