"""Deterministic tests for the server-side VidLink playback token."""

from __future__ import annotations

import base64
import struct

import pytest
from nacl.bindings import crypto_secretbox_open

from mediaflow_proxy.extractors.vidlink_token import TOKEN_TTL, generate_adv_token

_TOKEN_KEY = bytes.fromhex(
    "c75136c5668bbfe65a7ecad431a745db68b5f381555b38d8f6c699449cf11fcd"
)


def test_generate_adv_token_matches_wasm_layout():
    issued_at = 1_795_382_345
    token = generate_adv_token("786892", issued_at=issued_at)
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))

    assert len(raw) == 54  # 24-byte nonce + 14-byte message + 16-byte MAC
    assert raw[:24] == b"\x00" * 24
    message = crypto_secretbox_open(raw[24:], raw[:24], _TOKEN_KEY)
    assert message == b"786892\x00\x00\x00\x00" + struct.pack(
        ">I", issued_at + TOKEN_TTL
    )


def test_generate_adv_token_uses_raw_id_without_padding():
    token = generate_adv_token("550", issued_at=1_795_382_345)
    raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    message = crypto_secretbox_open(raw[24:], raw[:24], _TOKEN_KEY)
    assert message == b"550\x00\x00\x00\x00" + struct.pack(">I", 1_795_382_345 + 120)


def test_generate_adv_token_rejects_invalid_id():
    with pytest.raises(ValueError):
        generate_adv_token("")
