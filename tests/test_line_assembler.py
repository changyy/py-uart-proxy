"""S3: line assembly."""

from __future__ import annotations

from uart_proxy.core.line_assembler import LineAssembler


def test_splits_on_newline_and_strips_cr():
    asm = LineAssembler()
    assert asm.feed(b"a\r\nb") == [b"a"]
    assert asm.has_pending
    assert asm.flush() == b"b"
    assert not asm.has_pending


def test_multiple_lines_in_one_chunk():
    asm = LineAssembler()
    assert asm.feed(b"one\ntwo\nthree") == [b"one", b"two"]
    assert asm.flush() == b"three"


def test_flush_empty_returns_none():
    asm = LineAssembler()
    assert asm.flush() is None


def test_line_split_across_chunks():
    asm = LineAssembler()
    assert asm.feed(b"hel") == []
    assert asm.feed(b"lo\n") == [b"hello"]
