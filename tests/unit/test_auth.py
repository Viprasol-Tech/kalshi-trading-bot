"""Tests for Kalshi RSA-PSS request signing."""

from __future__ import annotations

import base64

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.exchange.auth import (
    build_auth_headers,
    sign_pss,
    signature_message,
)


def _make_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_signature_message_format() -> None:
    msg = signature_message(1234567890, "post", "/trade-api/v2/markets")
    assert msg == "1234567890POST/trade-api/v2/markets"


def test_sign_pss_is_verifiable() -> None:
    key = _make_key()
    message = signature_message(1700000000000, "GET", "/trade-api/v2/portfolio/balance")
    signature_b64 = sign_pss(key, message)

    # A valid PSS signature must verify against the public key.
    key.public_key().verify(
        base64.b64decode(signature_b64),
        message.encode(),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_build_auth_headers_keys() -> None:
    key = _make_key()
    headers = build_auth_headers("key-id-123", key, 1700000000000, "GET", "/trade-api/v2/markets")
    assert headers["KALSHI-ACCESS-KEY"] == "key-id-123"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    assert headers["KALSHI-ACCESS-SIGNATURE"]
