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
    def __init__(self, *, cr_ends_line: bool = False) -> None:
        """``cr_ends_line`` for what a person types: Enter on a serial console
        is a bare ``\r`` (``--eol cr``, and every keystroke in character mode),
        which would otherwise never finish a line. Device output keeps the
        default, where a bare ``\r`` is a repaint, not a line end.
        """
        self._buf = bytearray()
        self._cr_ends_line = cr_ends_line
        self._after_cr = False   # swallow the \n of a \r\n split across feeds

    def feed(self, data: bytes) -> list[bytes]:
        """Append ``data`` and return any complete lines (without terminators)."""
        if self._cr_ends_line:
            return self._feed_cr(data)
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

    def _feed_cr(self, data: bytes) -> list[bytes]:
        lines: list[bytes] = []
        for byte in data:
            if byte == 0x0A and self._after_cr:   # the \n of \r\n
                self._after_cr = False
                continue
            self._after_cr = byte == 0x0D
            if byte in (0x0D, 0x0A):
                lines.append(bytes(self._buf))
                self._buf.clear()
            else:
                self._buf.append(byte)
        return lines

    @property
    def has_pending(self) -> bool:
        return len(self._buf) > 0

    @property
    def pending(self) -> bytes:
        """The partial line so far, without consuming it (a prompt, say)."""
        return bytes(self._buf).rstrip(b"\r")

    def flush(self) -> bytes | None:
        """Return and clear any buffered partial line, or ``None`` if empty."""
        if not self._buf:
            return None
        raw = bytes(self._buf).rstrip(b"\r")
        self._buf.clear()
        self._after_cr = False
        return raw
