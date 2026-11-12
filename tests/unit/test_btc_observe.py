"""Observation-only collection tests use isolated accounts and mocked public reads."""

from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import respx

from kalshi_bot import btc_observe
from kalshi_bot.paper import PaperBroker
from tests.unit.test_btc import BOOK, MARKET_DATA, NOW, SERIES_DATA


def test_preserves_subcent_books() -> None:
    row = btc_observe.book_fields(BOOK)
    assert row["yes_bid"] == 0.405
    assert row["yes_bid_depth"] == 12.99
    assert row["no_ask"] == pytest.approx(0.595)


def test_partial_books_preserve_available_side_without_inventing_quotes() -> None:
    row = btc_observe.book_fields(
        {
            "orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.9400", "1.00"]]},
        }
    )
    assert row["yes_bid"] is None
    assert row["yes_ask"] == pytest.approx(0.06)
    assert row["no_bid_depth"] == 1
    assert row["no_ask"] is None


@pytest.mark.asyncio
async def test_observer_does_not_mutate_account_or_construct_orders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = tmp_path / "paper.db"
    PaperBroker(ledger).close()
    before = btc_observe.ledger_state(ledger)
    monkeypatch.setattr(btc_observe.time, "time", lambda: NOW)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Observation-only code must not trade.")

    monkeypatch.setattr(PaperBroker, "submit", forbidden)
    monkeypatch.setattr(PaperBroker, "settle", forbidden)
    headers = {"Date": format_datetime(datetime.fromtimestamp(NOW, UTC), usegmt=True)}
    base = "https://demo-api.kalshi.co/trade-api/v2"
    with respx.mock(assert_all_called=True) as api:
        api.get(base + "/series/KXBTC15M").mock(
            return_value=httpx.Response(
                200,
                json={"series": SERIES_DATA},
                headers=headers,
            )
        )
        api.get(base + "/markets").mock(
            return_value=httpx.Response(
                200,
                json={"markets": [MARKET_DATA], "cursor": ""},
                headers=headers,
            )
        )
        api.get(base + "/markets/" + MARKET_DATA["ticker"]).mock(
            return_value=httpx.Response(
                200,
                json={"market": MARKET_DATA},
                headers=headers,
            )
        )
        api.get(base + "/markets/" + MARKET_DATA["ticker"] + "/orderbook").mock(
            return_value=httpx.Response(200, json=BOOK, headers=headers),
        )
        api.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker").mock(
            return_value=httpx.Response(
                200,
                headers=headers,
                json={
                    "time": datetime.fromtimestamp(NOW, UTC).isoformat(),
                    "price": "73500",
                    "bid": "73499",
                    "ask": "73501",
                },
            ),
        )
        # Explicit test-only launch permission; network remains mocked/forbidden.
        monkeypatch.setattr(btc_observe, "require_collection_release", lambda: None)
        report = await btc_observe.run_capture(0.2, 1, tmp_path / "observations.db", ledger)
    assert report["status"] == "completed"
    assert report["valid_observations"] == 1
    assert report["entries_enabled"] is False
    assert report["calibration_ready"] is False
    assert report["paper_ledger_unchanged"]
    assert btc_observe.ledger_state(ledger) == before
