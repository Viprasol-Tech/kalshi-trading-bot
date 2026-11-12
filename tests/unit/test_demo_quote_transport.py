"""Modern transport tests replace TCP; no endpoint or credential is accessed."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

import kalshi_bot.demo_quote_ws as transport
from kalshi_bot.safety import SimulationOnlyError
from tests.unit.test_btc import NOW
from tests.unit.test_demo_quote_ws import TICKER, book


def quiet_logger() -> logging.Logger:
    logger = logging.Logger("offline-probe-fixture")
    logger.disabled = True
    return logger


@pytest.mark.parametrize("status", [300, 301, 302, 303, 304, 307, 308, 399])
@pytest.mark.parametrize(
    "location",
    [
        None,
        transport.DEMO_WS_URL,
        "wss://external-api-ws.kalshi.com/trade-api/ws/v2",
        "https://example.invalid/redirect",
    ],
)
def test_every_redirect_is_rejected_without_returning_a_uri(status: int, location: str | None):
    headers = Headers() if location is None else Headers({"Location": location})
    error = InvalidStatus(Response(status, "Redirect", headers))
    connector = transport.NoRedirectConnect(
        transport.DEMO_WS_URL, proxy=None, logger=quiet_logger()
    )
    refused = connector.process_redirect(error)
    assert isinstance(refused, SimulationOnlyError)
    assert str(refused) == "Demo WebSocket redirects are forbidden."


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_nonredirect_handshake_failures_preserve_the_failure(status: int):
    error = InvalidStatus(Response(status, "Rejected", Headers()))
    connector = transport.NoRedirectConnect(
        transport.DEMO_WS_URL, proxy=None, logger=quiet_logger()
    )
    assert connector.process_redirect(error) is error


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [302, 307])
@pytest.mark.parametrize("same_origin", [False, True])
async def test_real_modern_connect_path_aborts_after_one_redirect(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    status: int,
    same_origin: bool,
):
    location = transport.DEMO_WS_URL if same_origin else "wss://example.invalid/"
    error = InvalidStatus(Response(status, "Redirect", Headers({"Location": location})))
    socket_transport = SimpleNamespace(abort=Mock())
    connection = SimpleNamespace(
        handshake=AsyncMock(side_effect=error),
        transport=socket_transport,
        start_keepalive=Mock(),
    )
    connector = transport.NoRedirectConnect(
        transport.DEMO_WS_URL,
        additional_headers={},
        proxy=None,
        open_timeout=1,
        logger=quiet_logger(),
    )
    opener = AsyncMock(return_value=connection)
    monkeypatch.setattr(connector, "open_tcp_connection", opener)
    with pytest.raises(SimulationOnlyError, match="redirects"):
        await connector
    opener.assert_awaited_once()
    connection.handshake.assert_awaited_once()
    socket_transport.abort.assert_called_once()
    connection.start_keepalive.assert_not_called()
    assert connector.uri == transport.DEMO_WS_URL
    assert connector.proxy is None
    assert not caplog.records


@pytest.fixture(scope="module")
def ephemeral_key() -> rsa.RSAPrivateKey:
    # Generated only in memory, never loaded, serialized, printed or persisted.
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class StubSocket:
    def __init__(self, frames: list[str]):
        self.frames = iter(frames)
        self.commands: list[dict] = []
        self.closed = False
        self.waiting = asyncio.Event()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def send(self, message: str):
        self.commands.append(json.loads(message))

    async def recv(self):
        frame = next(self.frames, None)
        if frame is None:
            self.waiting.set()
            await asyncio.Event().wait()
        return frame


def stub_probe(monkeypatch: pytest.MonkeyPatch, frames: list[str]):
    socket = StubSocket(frames)
    options = {}

    def create(uri, **kwargs):
        options.update(uri=uri, **kwargs)
        return socket

    monkeypatch.setattr(transport, "NoRedirectConnect", create)
    monkeypatch.setattr(transport, "build_auth_headers", lambda *args: {})
    monkeypatch.setattr(transport.time, "time", lambda: NOW)
    return socket, options


@pytest.mark.asyncio
async def test_stream_uses_modern_direct_demo_transport_and_closes_on_consumer_stop(
    monkeypatch: pytest.MonkeyPatch, ephemeral_key: rsa.RSAPrivateKey
):
    socket, options = stub_probe(
        monkeypatch, [json.dumps({"type": "subscribed"}), json.dumps(book())]
    )
    events = transport.stream(
        [TICKER], demo_key_id="offline-fixture", demo_private_key=ephemeral_key, approved=True
    )
    event = await anext(events)
    assert event.ticker == TICKER
    assert event.event_at is None and not event.whole_book_freshness_verified
    assert socket.commands == [transport.subscription([TICKER])]
    assert options["uri"] == transport.DEMO_WS_URL
    assert options["additional_headers"] == {} and "extra_headers" not in options
    assert options["proxy"] is None
    assert options["open_timeout"] == 10
    assert options["ping_interval"] == 20 and options["ping_timeout"] == 10
    assert options["close_timeout"] == 2 and options["max_size"] == 1_048_576
    assert options["logger"].disabled and not options["logger"].propagate
    await events.aclose()
    assert socket.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame", ["[]", "null", "true", '"text"', "{bad", '{"type":"error","msg":"withheld"}']
)
async def test_bad_or_rejected_message_closes_without_execution(
    monkeypatch: pytest.MonkeyPatch, ephemeral_key: rsa.RSAPrivateKey, frame: str
):
    socket, _ = stub_probe(monkeypatch, [frame])
    events = transport.stream(
        [TICKER], demo_key_id="offline-fixture", demo_private_key=ephemeral_key, approved=True
    )
    with pytest.raises(ValueError):
        await anext(events)
    assert socket.closed
    assert socket.commands == [transport.subscription([TICKER])]


@pytest.mark.asyncio
async def test_global_deadline_closes_a_quiet_stream(
    monkeypatch: pytest.MonkeyPatch, ephemeral_key: rsa.RSAPrivateKey
):
    socket, _ = stub_probe(monkeypatch, [])
    events = transport.stream(
        [TICKER],
        demo_key_id="offline-fixture",
        demo_private_key=ephemeral_key,
        approved=True,
        duration_seconds=1,
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(anext(events), timeout=3)
    assert socket.closed
    assert len(socket.commands) == 1


@pytest.mark.asyncio
async def test_cancellation_closes_without_reconnect(
    monkeypatch: pytest.MonkeyPatch, ephemeral_key: rsa.RSAPrivateKey
):
    socket, _ = stub_probe(monkeypatch, [])
    events = transport.stream(
        [TICKER], demo_key_id="offline-fixture", demo_private_key=ephemeral_key, approved=True
    )
    task = asyncio.create_task(anext(events))
    await asyncio.wait_for(socket.waiting.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert socket.closed and len(socket.commands) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [0, 301, True, 1.5])
async def test_invalid_duration_refuses_before_signing_or_connection(
    monkeypatch: pytest.MonkeyPatch, ephemeral_key: rsa.RSAPrivateKey, duration
):
    signer = Mock(side_effect=AssertionError("Signing must not occur."))
    opener = Mock(side_effect=AssertionError("Connection must not occur."))
    monkeypatch.setattr(transport, "build_auth_headers", signer)
    monkeypatch.setattr(transport, "NoRedirectConnect", opener)
    with pytest.raises(ValueError, match="at most"):
        await anext(
            transport.stream(
                [TICKER],
                demo_key_id="offline-fixture",
                demo_private_key=ephemeral_key,
                approved=True,
                duration_seconds=duration,
            )
        )
    signer.assert_not_called()
    opener.assert_not_called()
