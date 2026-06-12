"""S6: proxy wire protocol helpers."""

from __future__ import annotations

import pytest

from uart_proxy.proxy.protocol import (
    Role,
    decode_message,
    encode_message,
    parse_auth_spec,
)


def test_encode_decode_roundtrip():
    msg = {"type": "rx", "hex": "48656c6c6f", "text": "Hello"}
    line = encode_message(msg)
    assert line.endswith(b"\n")
    assert decode_message(line.rstrip(b"\n")) == msg


def test_decode_rejects_non_object():
    with pytest.raises(ValueError):
        decode_message(b"[1, 2, 3]")
    with pytest.raises(ValueError):
        decode_message(b"not json")


def test_parse_auth_spec_defaults_to_full():
    assert parse_auth_spec("123456") == ("123456", Role.FULL)


def test_parse_auth_spec_role():
    assert parse_auth_spec("000000:readonly") == ("000000", Role.READONLY)
    assert parse_auth_spec("111:full") == ("111", Role.FULL)


def test_parse_auth_spec_bad_role():
    with pytest.raises(ValueError):
        parse_auth_spec("000000:superuser")
