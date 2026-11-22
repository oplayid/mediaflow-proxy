"""Server-side generator for VidLink's ``adv_token`` playback token.

VidLink mints the token in the browser with a small Go/WASM module. The module
calls ``sodium.crypto_secretbox_easy`` with a fixed key and an all-zero nonce.
Its plaintext is the TMDB id, four zero bytes and a big-endian Unix expiry.
Reimplementing it here keeps the browser (and the WASM challenge) out of the
gateway while still producing a token the playback API accepts.
"""

from __future__ import annotations

import base64
import struct
import time

from nacl.bindings import crypto_secretbox

# Extracted from the public /fu.wasm module served by vidlink.pro.
_TOKEN_KEY = bytes.fromhex(
    "c75136c5668bbfe65a7ecad431a745db68b5f381555b38d8f6c699449cf11fcd"
)
_NONCE = b"\x00" * 24
# The WASM generator issues a short-lived token; match its two-minute window.
TOKEN_TTL = 120


def generate_adv_token(media_id: str, *, issued_at: int | None = None) -> str:
    """Return a base64url ``adv_token`` for ``media_id``.

    ``issued_at`` is exposed for deterministic tests. Production callers use
    the current Unix time plus :data:`TOKEN_TTL`.
    """
    identifier = str(media_id).strip().encode("ascii")
    if not identifier or len(identifier) > 64:
        raise ValueError("invalid VidLink media id")

    now = int(time.time()) if issued_at is None else int(issued_at)
    expiry = now + TOKEN_TTL
    message = identifier + b"\x00" * 4 + struct.pack(">I", expiry & 0xFFFFFFFF)
    sealed = crypto_secretbox(message, _NONCE, _TOKEN_KEY)
    return base64.urlsafe_b64encode(_NONCE + sealed).decode("ascii").rstrip("=")
