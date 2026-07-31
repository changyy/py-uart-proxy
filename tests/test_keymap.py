"""S19: keystrokes to wire bytes, and the command prefix."""

from __future__ import annotations

import pytest

from uart_proxy.ui.keymap import (
    BACKSPACE_BS,
    BACKSPACE_DEL,
    DEFAULT_PREFIX,
    key_to_bytes,
    normalise_prefix,
    prefix_label,
)


# ── what a serial console expects ───────────────────────────────────────────


@pytest.mark.parametrize(
    "key, character, expected, why",
    [
        ("a", "a", b"a", "printable"),
        ("Z", "Z", b"Z", "printable, case kept"),
        ("space", " ", b" ", "space is a byte like any other"),
        ("ctrl+c", None, b"\x03", "INTR — the reason character mode exists"),
        ("ctrl+d", None, b"\x04", "EOF"),
        ("ctrl+z", None, b"\x1a", "SUSP"),
        ("ctrl+a", None, b"\x01", "readline line-start, must reach the device"),
        ("ctrl+backslash", None, b"\x1c", "QUIT"),
        ("ctrl+right_square_bracket", None, b"\x1d", "the prefix, sent literally"),
        ("tab", "\t", b"\t", "completion"),
        ("escape", "\x1b", b"\x1b", "ESC"),
        ("up", None, b"\x1b[A", "history — an escape sequence, sent whole"),
        ("down", None, b"\x1b[B", ""),
        ("right", None, b"\x1b[C", ""),
        ("left", None, b"\x1b[D", ""),
        ("delete", None, b"\x1b[3~", ""),
        ("f1", None, b"\x1bOP", ""),
    ],
)
def test_keys_become_the_bytes_a_terminal_would_send(key, character, expected, why):
    assert key_to_bytes(key, character) == expected, why


def test_enter_sends_the_configured_line_ending():
    assert key_to_bytes("enter", None, eol=b"\r") == b"\r"
    assert key_to_bytes("enter", None, eol=b"\r\n") == b"\r\n"
    assert key_to_bytes("enter", None, eol=b"") == b""


def test_backspace_sends_del_not_bs():
    """Unix consoles and readline expect DEL; BS shows up as ^H on many devices,
    which is why PuTTY and minicom default to DEL too."""
    assert key_to_bytes("backspace", "\x08") == BACKSPACE_DEL
    assert key_to_bytes("backspace", "\x08", backspace=BACKSPACE_BS) == BACKSPACE_BS


def test_keys_with_nothing_to_send_are_ignored():
    """Better to send nothing than a byte the device has to interpret."""
    assert key_to_bytes("f12", None) is None
    assert key_to_bytes("ctrl+shift+alt+meta", None) is None


def test_non_ascii_uses_the_session_encoding():
    assert key_to_bytes("é", "é", encoding="utf-8") == "é".encode()
    assert key_to_bytes("é", "é", encoding="latin-1") == "é".encode("latin-1")


# ── the prefix ──────────────────────────────────────────────────────────────


def test_the_default_is_telnets_escape():
    """Not screen's Ctrl+A or tmux's Ctrl+B: both are readline keys a serial
    console needs (line-start, back-one-char). Ctrl+] means nothing to readline
    and nothing to flow control."""
    assert DEFAULT_PREFIX == "ctrl+right_square_bracket"
    assert prefix_label(DEFAULT_PREFIX) == "Ctrl+]"


@pytest.mark.parametrize(
    "spec, expected",
    [
        ("ctrl+]", "ctrl+right_square_bracket"),
        ("ctrl-]", "ctrl+right_square_bracket"),
        ("^]", "ctrl+right_square_bracket"),
        ("CTRL+]", "ctrl+right_square_bracket"),
        ("ctrl+a", "ctrl+a"),
        ("ctrl+backslash", "ctrl+backslash"),
        ("ctrl+\\", "ctrl+backslash"),
        ("ctrl+right_square_bracket", "ctrl+right_square_bracket"),
    ],
)
def test_a_prefix_can_be_written_the_obvious_ways(spec, expected):
    assert normalise_prefix(spec) == expected


@pytest.mark.parametrize("spec", ["]", "a", "alt+x", "ctrl+f13", "", "ctrl+"])
def test_an_unusable_prefix_is_refused_not_guessed(spec):
    """Silently picking a different key would leave the user pressing something
    that does nothing."""
    with pytest.raises(ValueError):
        normalise_prefix(spec)


def test_labels_read_the_way_people_write_them():
    assert prefix_label("ctrl+a") == "Ctrl+A"
    assert prefix_label("ctrl+backslash") == "Ctrl+\\"
