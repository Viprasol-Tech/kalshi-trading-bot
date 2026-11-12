"""The complete test suite is offline and may never reach an external socket."""

import socket
import sys
from types import CodeType

import pytest


def internal_socketpair(
    platform: str, code: CodeType | None, address: object, trusted: tuple[CodeType, ...]
) -> bool:
    """Permit ONLY CPython's Windows socketpair implementation, never HTTP/test sockets."""
    return (
        platform == "win32"
        and code in trusted
        and isinstance(address, tuple)
        and len(address) >= 2
        and address[0] in ("127.0.0.1", "::1")
        and type(address[1]) is int
        and 0 < address[1] <= 65535
    )


@pytest.fixture
def offline_execution_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the old simulator only in isolated unit fixtures, not release approval.

    New safety tests do NOT request this fixture. Never patch this into runtime.
    """
    monkeypatch.setattr("kalshi_bot.paper.PaperBroker._require_entries", lambda self: None)
    monkeypatch.setattr("kalshi_bot.btc_strategy.require_entry_release", lambda: None)
    monkeypatch.setattr("kalshi_bot.btc_strategy.require_collection_release", lambda: None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    original = socket.socket.connect
    trusted = tuple(
        code
        for function in (socket.socketpair, getattr(socket, "_fallback_socketpair", None))
        if isinstance(code := getattr(function, "__code__", None), CodeType)
    )

    def blocked(self: socket.socket, address: object) -> None:
        if internal_socketpair(sys.platform, sys._getframe(1).f_code, address, trusted):
            original(self, address)
            return
        raise AssertionError("External network access is forbidden in tests.")

    def blocked_ex(*args: object, **kwargs: object) -> None:
        raise AssertionError("External network access is forbidden in tests.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked_ex)
