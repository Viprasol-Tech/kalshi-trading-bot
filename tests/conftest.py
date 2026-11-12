"""The complete test suite is offline and may never reach an external socket."""

import socket

import pytest


@pytest.fixture
def offline_execution_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise the old simulator only in isolated unit fixtures, not release approval.

    New safety tests do NOT request this fixture. Never patch this into runtime.
    """
    monkeypatch.setattr("kalshi_bot.paper.PaperBroker._require_entries", lambda self: None)
    monkeypatch.setattr("kalshi_bot.btc_strategy.require_entry_release", lambda: None)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*args: object, **kwargs: object) -> None:
        raise AssertionError("External network access is forbidden in tests.")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
