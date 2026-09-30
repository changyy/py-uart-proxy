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


# ── what a person types: Enter is often a bare CR ───────────────────────────


def test_typed_cr_ends_a_line():
    """`connect` defaults to --eol cr: without this no typed line ever finished,
    so TX lines never reached the log view, --log-tx or tx_echo."""
    asm = LineAssembler(cr_ends_line=True)
    assert asm.feed(b"ls\rpwd\r") == [b"ls", b"pwd"]


def test_typed_crlf_is_one_line_not_two():
    asm = LineAssembler(cr_ends_line=True)
    assert asm.feed(b"ls\r\npwd\r\n") == [b"ls", b"pwd"]


def test_typed_crlf_split_across_writes_is_still_one_line():
    asm = LineAssembler(cr_ends_line=True)
    assert asm.feed(b"ls\r") == [b"ls"]
    assert asm.feed(b"\npwd\n") == [b"pwd"]


def test_typed_lf_alone_ends_a_line():
    asm = LineAssembler(cr_ends_line=True)
    assert asm.feed(b"ls\n") == [b"ls"]


def test_an_empty_enter_is_an_empty_line():
    asm = LineAssembler(cr_ends_line=True)
    assert asm.feed(b"\r\r") == [b"", b""]


def test_keystrokes_one_at_a_time_make_one_line():
    asm = LineAssembler(cr_ends_line=True)
    out = []
    for byte in b"uname -a\r":
        out += asm.feed(bytes([byte]))
    assert out == [b"uname -a"]


def test_device_output_still_treats_a_bare_cr_as_a_repaint():
    """RX keeps the default: progress bars redraw with \\r, they don't end lines."""
    asm = LineAssembler()
    assert asm.feed(b"10%\r50%\r100%\n") == [b"10%\r50%\r100%"]
