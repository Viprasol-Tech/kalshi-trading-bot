"""Kalshi API request signing.

Kalshi authenticates every request with an **API key ID** plus an **RSA private
key**. Each request carries three headers:

* ``KALSHI-ACCESS-KEY``       — the API key ID (UUID).
* ``KALSHI-ACCESS-TIMESTAMP`` — current Unix time in **milliseconds**.
* ``KALSHI-ACCESS-SIGNATURE`` — base64 ``RSA-PSS(SHA-256)`` signature of the
  string ``timestamp + METHOD + path`` (path includes the ``/trade-api/v2``
  prefix and **excludes** the query string).

References:
    https://docs.kalshi.com/getting_started/quick_start_authenticated_requests

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import base64
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes


def load_private_key(pem_path: str | Path, password: bytes | None = None) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file on disk.

    The private key file must **never** be committed to version control. See
    ``.gitignore`` and ``SECURITY.md``.
    """
    data = Path(pem_path).read_bytes()
    key: PrivateKeyTypes = serialization.load_pem_private_key(data, password=password)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise TypeError("Kalshi requires an RSA private key; got a different key type.")
    return key


def sign_pss(private_key: rsa.RSAPrivateKey, message: str) -> str:
    """Return the base64-encoded ``RSA-PSS(SHA-256)`` signature for ``message``.

    Salt length equals the digest length (32 bytes for SHA-256), matching
    Kalshi's ``PSS.DIGEST_LENGTH`` requirement. RSA-PSS is randomised, so the
    signature differs on every call even for identical input — this is expected.
    """
    signature = private_key.sign(
        message.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def signature_message(timestamp_ms: int, method: str, path: str) -> str:
    """Build the exact string Kalshi expects to be signed.

    ``path`` must include the ``/trade-api/v2`` prefix and exclude any query
    string. Concatenation is ``timestamp + METHOD + path`` with no separators.
    """
    return f"{timestamp_ms}{method.upper()}{path}"


def build_auth_headers(
    api_key_id: str,
    private_key: rsa.RSAPrivateKey,
    timestamp_ms: int,
    method: str,
    path: str,
) -> dict[str, str]:
    """Build the three Kalshi auth headers for a single request.

    Args:
        api_key_id: The API key ID (UUID) from the Kalshi dashboard.
        private_key: Loaded RSA private key (see :func:`load_private_key`).
        timestamp_ms: Current Unix time in milliseconds.
        method: HTTP method, e.g. ``"GET"`` or ``"POST"``.
        path: Request path including ``/trade-api/v2`` and excluding the query.
    """
    message = signature_message(timestamp_ms, method, path)
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "KALSHI-ACCESS-SIGNATURE": sign_pss(private_key, message),
    }
