"""
Reassemble a byte stream into lines.

UART data arrives in arbitrary chunks; line-oriented consumers (the
timestamped logs and most plugins) need whole lines. ``LineAssembler`` buffers
bytes and yields complete lines as they are terminated by ``\\n`` (a trailing
``\\r`` is stripped). Partial data can be force-emitted with :meth:`flush`,
which the session uses to surface prompts that lack a trailing newline (e.g.
``login: ``) after a short idle period.
"""

from __future__ import annotations


class LineAssembler:
    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        """Append ``data`` and return any complete lines (without terminators)."""
        self._buf.extend(data)
        lines: list[bytes] = []
        while True:
            idx = self._buf.find(b"\n")
            if idx < 0:
                break
            raw = bytes(self._buf[:idx]).rstrip(b"\r")
            del self._buf[: idx + 1]
            lines.append(raw)
        return lines

    @property
    def has_pending(self) -> bool:
        return len(self._buf) > 0

    def flush(self) -> bytes | None:
        """Return and clear any buffered partial line, or ``None`` if empty."""
        if not self._buf:
            return None
        raw = bytes(self._buf).rstrip(b"\r")
        self._buf.clear()
        return raw
