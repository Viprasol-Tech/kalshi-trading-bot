"""Offline only: real-response fragments plus explicitly synthetic WS drills."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx

from kalshi_bot.btc_collect import current_candidates, quote_timestamp
from kalshi_bot.btc_observe import book_fields
from kalshi_bot.btc_rules import administrative_expiration, verify_market
from kalshi_bot.demo_quote_adapter import OfflineQuoteAdapter
from kalshi_bot.demo_quote_ws import DemoBookSession, ResnapshotRequired
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.multi_feed import MultiFeed
from kalshi_bot.multi_market import PaperEngine
from kalshi_bot.safety import SimulationOnlyError
from tests.unit.test_btc import MARKET_DATA, NOW, SERIES_DATA
from tests.unit.test_demo_quote_ws import TICKER, book, delta

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures/demo_preflight_failures.json").read_text()
)


def test_observed_catalog_closed_entries_never_become_active_candidates() -> None:
    observed = {r["ticker"]: r for r in FIXTURE["catalog_markets"]}
    assert not current_candidates(observed, 1794511809)
    for raw, status in zip(FIXTURE["catalog_markets"], FIXTURE["detail_states"], strict=True):
        with pytest.raises(ValueError, match="lifecycle"):
            verify_market({**raw, "status": status}, SERIES_DATA, now=1794511809)


@pytest.mark.parametrize("index", [0, 1])
def test_observed_books_keep_missing_data_and_timestamp_missing(index: int) -> None:
    body = FIXTURE["books"][index]
    fields = book_fields(body)
    assert fields["yes_bid"] is None and fields["no_ask"] is None
    assert quote_timestamp(body, 1794511809)["quote_timestamp_status"] == "unavailable"
    if index:
        assert fields["yes_ask"] == 0.47
        assert fields["no_bid"] == 0.53
    else:
        assert fields["yes_ask"] is None and fields["no_bid"] is None


def test_current_expiration_schema_is_explicit_and_preserves_legacy() -> None:
    original = copy.deepcopy(MARKET_DATA)
    legacy = verify_market(original, SERIES_DATA, now=NOW)
    current = {k: v for k, v in original.items() if k != "expiration_time"}
    current["latest_expiration_time"] = original["expiration_time"]
    verified = verify_market(current, SERIES_DATA, now=NOW)
    assert verified.administrative_expiration == legacy.administrative_expiration
    assert verified.administrative_expiration_field == "latest_expiration_time"
    assert legacy.administrative_expiration_field == "expiration_time"
    assert current.get("expiration_time") is None
    assert original == MARKET_DATA


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"expected_expiration_time": MARKET_DATA["expiration_time"]},
        {"latest_expiration_time": True},
        {"expiration_time": ""},
        {"latest_expiration_time": "not-a-time"},
        {"latest_expiration_time": "2026-11-11T15:10:00"},
        {
            "expiration_time": MARKET_DATA["expiration_time"],
            "latest_expiration_time": "2026-12-01T00:00:00Z",
        },
    ],
)
def test_missing_invalid_ambiguous_expiration_never_guessed(raw: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        administrative_expiration(raw)


@pytest.mark.parametrize("status", ["closed", "determined", "disputed", "amended", "inactive"])
def test_new_expiration_field_never_relaxes_lifecycle(status: str) -> None:
    raw = {k: v for k, v in MARKET_DATA.items() if k != "expiration_time"}
    raw.update(latest_expiration_time=MARKET_DATA["expiration_time"], status=status)
    with pytest.raises(ValueError, match="lifecycle"):
        verify_market(raw, SERIES_DATA, now=NOW)


@pytest.mark.parametrize("strike", [None, 0, -1, "not-a-strike", True])
def test_new_expiration_field_never_relaxes_floor_strike(strike: object) -> None:
    raw = {k: v for k, v in MARKET_DATA.items() if k != "expiration_time"}
    raw.update(latest_expiration_time=MARKET_DATA["expiration_time"], floor_strike=strike)
    with pytest.raises((ValueError, TypeError)):
        verify_market(raw, SERIES_DATA, now=NOW)


@pytest.mark.asyncio
async def test_closed_listing_filters_before_any_detail_requests(tmp_path: Path) -> None:
    e = PaperEngine(tmp_path / "trial.db")
    try:
        feed = MultiFeed(e)
        with respx.mock(assert_all_called=True) as api:
            api.get("https://demo-api.kalshi.co/trade-api/v2/markets").respond(
                200, json={"markets": FIXTURE["catalog_markets"], "cursor": ""}
            )
            async with KalshiClient("https://demo-api.kalshi.co") as client:
                assert not await feed.scan(client)
            assert len(api.calls) == 1
        assert e.db.execute("SELECT COUNT(*) FROM mm_candidates").fetchone()[0] == 2
        assert all(
            r[0] == "catalog_not_currently_active"
            for r in e.db.execute("SELECT reason FROM mm_candidates")
        )
        assert not e.db.execute("SELECT 1 FROM mm_orders").fetchone()
    finally:
        e.close()


def test_snapshot_and_fresh_delta_cannot_release_whole_book() -> None:
    a = OfflineQuoteAdapter([TICKER])
    first = a.consume(book(), received_at=NOW, monotonic=1)
    assert first["latest_message"]["event_at"] is None
    assert first["latest_message"]["sending_at"] == NOW
    row = a.consume(delta(), received_at=NOW, monotonic=2)
    assert row["delta_event_fresh"]
    assert row["quote_sides"]["yes"]["level_event_fresh"]
    assert not row["quote_sides"]["no"]["level_event_fresh"]
    assert row["quote_sides"]["no"]["level_event_at"] is None
    assert not row["whole_book_freshness_verified"]
    assert not row["execution_eligible"] and not row["entries_enabled"]
    with pytest.raises(SimulationOnlyError, match="cannot release"):
        a.as_execution_book()


def test_fresh_send_time_never_dates_a_snapshot_or_delta() -> None:
    a = OfflineQuoteAdapter([TICKER])
    msg = book()
    msg["msg"]["ts_ms"] = int(NOW * 1000)  # Not documented for snapshots.
    snapshot = a.consume(msg, received_at=NOW, monotonic=1)
    assert snapshot["latest_message"]["event_at"] is None
    update = delta()
    del update["msg"]["ts_ms"]
    row = a.consume(update, received_at=NOW, monotonic=2)
    assert row["latest_message"]["sending_at"] == NOW
    assert not row["delta_event_fresh"]
    assert row["quote_sides"]["yes"]["level_event_at"] is None


def test_untouched_levels_and_zero_delta_never_rejuvenated() -> None:
    a = OfflineQuoteAdapter([TICKER])
    a.consume(book(), received_at=NOW, monotonic=1)
    a.consume(delta(), received_at=NOW, monotonic=2)
    msg = delta(4)
    msg["sending_ts_ms"] = int((NOW + 4) * 1000)
    msg["msg"].update(ts_ms=int((NOW + 4) * 1000), delta_fp="0.00")
    row = a.consume(msg, received_at=NOW + 4, monotonic=6)
    assert row["quote_sides"]["yes"]["level_event_at"] == NOW - 1
    stale = a.diagnose(TICKER, now=NOW + 6, monotonic=8)
    assert not stale["quote_sides"]["yes"]["level_event_fresh"]
    assert stale["quote_sides"]["no"]["level_event_at"] is None


def test_replacement_snapshot_resets_provenance_and_old_report_is_immutable() -> None:
    a = OfflineQuoteAdapter([TICKER])
    a.consume(book(), received_at=NOW, monotonic=1)
    old = a.consume(delta(), received_at=NOW, monotonic=2)
    replacement = book(4)
    replacement["msg"]["clock"] = 6
    replacement["msg"]["no_dollars_fp"] = []
    new = a.consume(replacement, received_at=NOW, monotonic=3)
    assert new["quote_sides"]["yes"]["level_event_at"] is None
    assert new["quote_sides"]["yes"]["ask"] is None
    assert old["quote_sides"]["yes"]["level_event_at"] == NOW - 1
    assert old["quote_sides"]["yes"]["ask"] == "0.45"


@pytest.mark.parametrize("change", [{"seq": 5}, {"sid": 2}, {"seq": 2}])
def test_adapter_failure_clears_all_quotes_and_cannot_resume(change: dict) -> None:
    a = OfflineQuoteAdapter([TICKER])
    a.consume(book(), received_at=NOW, monotonic=1)
    with pytest.raises(ResnapshotRequired):
        a.consume({**delta(), **change}, received_at=NOW, monotonic=2)
    assert not a.events and not a.level_times and not a.session.books
    assert not a.diagnose(TICKER, now=NOW, monotonic=2)["session_valid"]
    with pytest.raises(ResnapshotRequired):
        a.consume(book(6), received_at=NOW, monotonic=3)
    # Explicit new connection epoch may have lower exchange clocks.
    fresh = OfflineQuoteAdapter([TICKER])
    replacement = book(1)
    replacement["msg"]["clock"] = 0
    assert fresh.consume(replacement, received_at=NOW, monotonic=3)["session_valid"]


@pytest.mark.parametrize(
    "change",
    [
        {"ts_ms": int((NOW - 1) * 1000), "ts": "2026-01-01T00:00:00Z"},
        {"ts": []},
        {"ts_ms": True},
        {"ts_ms": -1},
        {"price_dollars": True},
        {"delta_fp": True},
        {"clock": 5},
        {"clock": 0},
    ],
)
def test_timestamp_conflicts_and_invalid_delta_payload_clear_session(change: dict) -> None:
    session = DemoBookSession([TICKER])
    session.consume(book(), received_at=NOW, monotonic=1)
    msg = delta()
    msg["msg"].update(change)
    with pytest.raises(ResnapshotRequired):
        session.consume(msg, received_at=NOW, monotonic=2)
    assert not session.books


def test_equivalent_legacy_and_millisecond_timestamps_are_allowed() -> None:
    session = DemoBookSession([TICKER])
    session.consume(book(), received_at=NOW, monotonic=1)
    msg = delta()
    msg["msg"]["ts"] = datetime.fromtimestamp(NOW - 1, UTC).isoformat()
    event = session.consume(msg, received_at=NOW, monotonic=2)
    assert event.event_at == NOW - 1 and event.event_field == "msg.ts_ms"


@pytest.mark.parametrize("now,mono", [(NOW - 1, 2), (NOW, 0), (NOW + 5, 2), (True, 2)])
def test_receive_clock_regression_and_discontinuity_invalidate(now: float, mono: float) -> None:
    session = DemoBookSession([TICKER])
    session.consume(book(), received_at=NOW, monotonic=1)
    with pytest.raises(ResnapshotRequired):
        session.consume(delta(), received_at=now, monotonic=mono)
    assert not session.books


@pytest.mark.asyncio
async def test_invalid_listings_cannot_starve_a_valid_rollover_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kalshi_bot import multi_feed
    from kalshi_bot.multi_market import Category
    from tests.unit.test_multi_market import NOW as MULTI_NOW
    from tests.unit.test_multi_market import market

    monkeypatch.setattr(multi_feed.time, "time", lambda: MULTI_NOW)
    c = market(Category.WEATHER)
    invalid = [{**c.raw, "ticker": f"EXPIRED-{i}", "status": "determined"} for i in range(12)]
    base = "https://demo-api.kalshi.co/trade-api/v2"
    e = PaperEngine(tmp_path / "trial.db")
    try:
        feed = MultiFeed(e)
        with respx.mock(assert_all_called=True) as api:
            api.get(base + "/markets").respond(
                200, json={"markets": [*invalid, c.raw], "cursor": ""}
            )
            api.get(base + "/events/" + c.event).respond(
                200, json={"event": {"event_ticker": c.event, "series_ticker": "TEST"}}
            )
            api.get(base + "/series/TEST").respond(200, json={"series": c.series})
            api.get(base + "/markets/" + c.ticker).respond(200, json={"market": c.raw})
            api.get(base + "/markets/" + c.ticker + "/orderbook").respond(
                200, json={"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}
            )
            async with KalshiClient("https://demo-api.kalshi.co") as client:
                assert not await feed.scan(client)
            assert len(api.calls) == 5  # No invalid candidate hydration calls.
        row = e.db.execute(
            "SELECT reason FROM mm_candidates WHERE ticker=?", (c.ticker,)
        ).fetchone()
        assert "timestamp" in row[0]
        assert not e.db.execute("SELECT 1 FROM mm_orders").fetchone()
    finally:
        e.close()


@pytest.mark.parametrize("field", ["yes_dollars_fp", "no_dollars_fp"])
@pytest.mark.parametrize("levels", [None, {}, "bad", [["0.40", True]], [["0.40", "1e20"]]])
def test_malformed_snapshot_shapes_or_depth_fail_closed(field: str, levels: object) -> None:
    a = OfflineQuoteAdapter([TICKER])
    msg = book()
    msg["msg"][field] = levels
    with pytest.raises(ResnapshotRequired):
        a.consume(msg, received_at=NOW, monotonic=1)
    assert a.session.failed and not a.events and not a.level_times
