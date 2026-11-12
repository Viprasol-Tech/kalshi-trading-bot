"""Quote-event provenance tests use synthetic messages only, no credentials."""

from __future__ import annotations

import copy

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import InvalidStatus
from websockets.http11 import Response

from kalshi_bot.demo_quote_ws import (
    DEMO_WS_URL,
    DemoBookSession,
    NoRedirectConnect,
    ResnapshotRequired,
    stream,
    subscription,
)
from kalshi_bot.safety import SimulationOnlyError
from tests.unit.test_btc import MARKET_DATA, NOW

TICKER = MARKET_DATA["ticker"]


def book(seq: int = 2) -> dict:
    return {
        "type": "orderbook_snapshot",
        "sid": 1,
        "seq": seq,
        "sending_ts_ms": int(NOW * 1000),
        "msg": {
            "market_ticker": TICKER,
            "clock": 5,
            "yes_dollars_fp": [["0.40", "4.50"]],
            "no_dollars_fp": [["0.55", "8.25"]],
        },
    }


def delta(seq: int = 3) -> dict:
    return {
        "type": "orderbook_delta",
        "sid": 1,
        "seq": seq,
        "sending_ts_ms": int(NOW * 1000),
        "msg": {
            "market_ticker": TICKER,
            "clock": 6,
            "side": "yes",
            "price_dollars": "0.40",
            "delta_fp": "1.25",
            "ts_ms": int((NOW - 1) * 1000),
        },
    }


def test_read_only_fixed_demo_endpoint_and_subscription() -> None:
    assert ".demo.kalshi.co/" in DEMO_WS_URL
    command = subscription([TICKER])
    assert command["params"]["channels"] == ["orderbook_delta"]
    assert command["params"]["update_interval_ms"] == 0
    assert command["params"]["use_yes_price"] is False
    for bad in ([], [TICKER, TICKER], ["OTHER"], ["KXBTC15M-../../orders"]):
        with pytest.raises(ValueError):
            subscription(bad)
    redirect = InvalidStatus(Response(302, "Redirect", Headers()))
    refused = NoRedirectConnect.process_redirect(object.__new__(NoRedirectConnect), redirect)
    assert isinstance(refused, SimulationOnlyError)
    assert "redirects" in str(refused)


@pytest.mark.asyncio
async def test_network_refused_before_credential_use_without_approval() -> None:
    # None is intentional: approval must be checked before key validation/network.
    with pytest.raises(SimulationOnlyError, match="approval"):
        await anext(stream([TICKER], demo_key_id="", demo_private_key=None))  # type: ignore[arg-type]


def test_snapshots_never_fabricate_event_time_or_missing_side() -> None:
    session = DemoBookSession([TICKER])
    message = book()
    del message["msg"]["no_dollars_fp"]
    event = session.consume(message, received_at=NOW, monotonic=1)
    assert event.event_at is None and event.sending_at == NOW
    assert not event.event_fresh and not event.whole_book_freshness_verified
    assert not session.books[TICKER]["no"]


def test_equal_exchange_clocks_preserved_distinct_sequences() -> None:
    session = DemoBookSession([TICKER])
    session.consume(book(), received_at=NOW, monotonic=1)
    first = session.consume(delta(), received_at=NOW, monotonic=2)
    second = session.consume(delta(4), received_at=NOW, monotonic=3)
    assert first.event_fresh and first.event_field == "msg.ts_ms"
    assert second.clock == first.clock and second.seq != first.seq
    assert str(session.books[TICKER]["yes"][next(iter(session.books[TICKER]["yes"]))]) == "7.00"
    assert not second.whole_book_freshness_verified


@pytest.mark.parametrize(
    "change",
    [
        {"seq": 5},
        {"seq": 2},
        {"sid": 2},
        {"msg": {**delta()["msg"], "clock": 4}},
        {"msg": {**delta()["msg"], "delta_fp": "-100"}},
        {"msg": {**delta()["msg"], "price_dollars": "NaN"}},
        {"msg": {**delta()["msg"], "side": "unknown"}},
        {"msg": {**delta()["msg"], "ts_ms": int((NOW + 1) * 1000)}},
    ],
)
def test_gap_or_corruption_invalidates_all_books(change: dict) -> None:
    session = DemoBookSession([TICKER])
    session.consume(book(), received_at=NOW, monotonic=1)
    with pytest.raises(ResnapshotRequired):
        session.consume({**delta(), **change}, received_at=NOW, monotonic=2)
    assert session.failed and not session.books
    with pytest.raises(ResnapshotRequired):
        session.consume(book(4), received_at=NOW, monotonic=3)


def test_missing_stale_and_deprecated_event_timestamps() -> None:
    for expected, stamp in ((None, None), (NOW - 10, int((NOW - 10) * 1000))):
        session = DemoBookSession([TICKER])
        session.consume(book(), received_at=NOW, monotonic=1)
        message = copy.deepcopy(delta())
        if stamp is None:
            del message["msg"]["ts_ms"]
        else:
            message["msg"]["ts_ms"] = stamp
        event = session.consume(message, received_at=NOW, monotonic=2)
        assert event.event_at == expected and not event.event_fresh
    session = DemoBookSession([TICKER])
    session.consume(book(), received_at=NOW, monotonic=1)
    message = delta()
    del message["msg"]["ts_ms"]
    message["msg"]["ts"] = "2026-11-11T15:09:59Z"
    event = session.consume(message, received_at=NOW, monotonic=2)
    assert event.event_field == "msg.ts" and event.event_at == NOW - 1
