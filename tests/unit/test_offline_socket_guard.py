"""Windows asyncio IPC exception is code-identity-specific, not a localhost allowance."""

from __future__ import annotations

import socket

import pytest

from tests.conftest import internal_socketpair


def marker() -> None:
    pass


@pytest.mark.parametrize(
    "address",
    [
        ("127.0.0.1", 12345),
        ("::1", 12345, 0, 0),
    ],
)
def test_only_trusted_windows_socketpair_loopback_is_allowed(address: tuple) -> None:
    trusted = (marker.__code__,)
    assert internal_socketpair("win32", marker.__code__, address, trusted)
    assert not internal_socketpair("linux", marker.__code__, address, trusted)
    assert not internal_socketpair("win32", None, address, trusted)
    assert not internal_socketpair("win32", marker.__code__, address, ())


@pytest.mark.parametrize(
    "address",
    [
        ("8.8.8.8", 443),
        ("demo-api.kalshi.co", 443),
        ("127.0.0.1", 0),
        ("127.0.0.1", True),
        "127.0.0.1",
    ],
)
def test_even_trusted_code_cannot_connect_external_or_malformed_address(address: object) -> None:
    assert not internal_socketpair("win32", marker.__code__, address, (marker.__code__,))


def test_direct_localhost_and_external_sockets_remain_blocked() -> None:
    with socket.socket() as client:
        for address in (("127.0.0.1", 12345), ("8.8.8.8", 443)):
            with pytest.raises(AssertionError, match="External network"):
                client.connect(address)
            with pytest.raises(AssertionError, match="External network"):
                client.connect_ex(address)
