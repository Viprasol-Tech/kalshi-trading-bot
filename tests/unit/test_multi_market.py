"""Synthetic state-machine drills are NOT real category model validation."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
import respx

from kalshi_bot import multi_market as mm
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.multi_dashboard import render, snapshot
from kalshi_bot.multi_feed import MultiFeed, run

NOW = 1_800_000_000.0


def iso(at: float) -> str:
    return datetime.fromtimestamp(at, UTC).isoformat()


def market(category: mm.Category, *, ticker: str = "A", closes: float = NOW + 600) -> mm.Contract:
    series = {
        "ticker": "TEST",
        "category": category.value,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Kalshi-defined official fixture"}],
    }
    raw = {
        "ticker": "TEST-E-" + ticker,
        "event_ticker": "TEST-E-" + ticker,
        "open_time": iso(NOW - 10),
        "close_time": iso(closes),
        "status": "open",
        "rules_primary": "Synthetic fixture rules, never a real trading model.",
    }
    # Keep the series/event/market hierarchy explicit.
    raw["event_ticker"] = "TEST-E" + ticker
    raw["ticker"] = raw["event_ticker"] + "-YES"
    return mm.Contract.verify(raw, series, at=NOW, now=NOW)


def book(c: mm.Contract, at: float = NOW, *, yes: float = 0.40) -> mm.Book:
    return mm.Book(c.ticker, c.ticker + ":" + str(at), at, at, yes, 0.98 - yes, 12, 12)


class FixtureModel:
    model_id = "synthetic-only-not-validated"

    def __init__(self, p: float = 0.95, group: str = "fixture"):
        self.p, self.group = p, group

    def predict(self, contract: mm.Contract, *, now: float) -> mm.Forecast:
        return mm.Forecast(self.p, 0.05, now, self.model_id, (self.group,))


@pytest.fixture
def released(monkeypatch: pytest.MonkeyPatch) -> None:
    # No runtime activation switch. Socket guard remains active.
    monkeypatch.setattr(mm, "require_model_release", lambda *args: None)


def engine(path: Path, categories: tuple[mm.Category, ...] = tuple(mm.Category)) -> mm.PaperEngine:
    return mm.PaperEngine(path, {c: FixtureModel(group=c.value) for c in categories})


def fill(e: mm.PaperEngine, c: mm.Contract) -> None:
    e.cycle([(c, book(c))], now=NOW)
    assert not e.db.execute("SELECT 1 FROM mm_fills").fetchone()
    e.cycle([(c, book(c, NOW + 1))], now=NOW + 1)


@pytest.mark.parametrize("category", tuple(mm.Category))
@pytest.mark.parametrize("p,side,outcome", [(0.95, "yes", "yes"), (0.05, "no", "no")])
def test_each_category_automatic_entry_and_authoritative_settlement(
    tmp_path: Path, released: None, category: mm.Category, p: float, side: str, outcome: str
) -> None:
    c = market(category)
    e = mm.PaperEngine(tmp_path / "trial.db", {category: FixtureModel(p)})
    try:
        e.activate()
        fill(e, c)
        assert e.db.execute("SELECT side FROM mm_intents").fetchone()[0] == side
        final = {
            **c.raw,
            "status": "finalized",
            "result": outcome,
            "settlement_ts": iso(c.closes + 10),
            "settlement_value_dollars": "1.0000" if outcome == "yes" else "0.0000",
        }
        e.settle(c, final, received_at=c.closes + 10, now=c.closes + 10)
        e.settle(c, final, received_at=c.closes + 10, now=c.closes + 10)
        assert e.db.execute("SELECT COUNT(*) FROM mm_settlements").fetchone()[0] == 1
        assert e.db.execute("SELECT realized FROM mm_intents").fetchone()[0] > 0
        assert (
            snapshot(tmp_path / "trial.db", now=c.closes + 10)["categories"][category.value][
                "win_rate"
            ]
            == 1
        )
    finally:
        e.close()


@pytest.mark.parametrize("category", tuple(mm.Category))
def test_each_category_later_quote_exit(
    tmp_path: Path, released: None, category: mm.Category
) -> None:
    c = market(category)
    e = engine(tmp_path / "trial.db")
    try:
        e.activate()
        fill(e, c)
        # Exit need not generate another probability in an unsupported BTC horizon.
        late = replace(c, metadata_at=c.closes - 50)
        e.cycle([(late, book(c, c.closes - 50, yes=0.70))], now=c.closes - 50)
        e.cycle([(late, book(c, c.closes - 49, yes=0.70))], now=c.closes - 49)
        assert e.db.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 2
        assert e.db.execute("SELECT status FROM mm_intents").fetchone()[0] == "closed"
        assert e.db.execute("SELECT COUNT(*) FROM mm_orders").fetchone()[0] == 2
    finally:
        e.close()


@pytest.mark.parametrize("category", tuple(mm.Category))
def test_real_release_cannot_be_faked_by_probability_or_passed_flag(
    tmp_path: Path, category: mm.Category
) -> None:
    c = market(category)
    d = mm.rank(c, book(c), FixtureModel(), now=NOW)
    assert d.side is None and "no reviewed model" in d.reason
    e = engine(tmp_path / "trial.db")
    try:
        with pytest.raises(mm.SimulationOnlyError):
            e.activate()
        e.cycle([(c, book(c))], now=NOW)
        assert e.db.execute("SELECT COUNT(*) FROM mm_orders").fetchone()[0] == 0
        assert e.db.execute("SELECT cash FROM mm_account").fetchone()[0] == 5000
    finally:
        e.close()


@pytest.mark.parametrize(
    "change",
    [
        {"event_at": NOW - 6},
        {"received_at": NOW - 6},
        {"event_at": NOW + 1},
        {"yes_bid": None},
        {"no_bid": None},
        {"yes_depth": 3.99},
        {"no_depth": 0.5},
        {"yes_bid": 0.8, "no_bid": 0.7},
        {"yes_bid": 0.2, "no_bid": 0.2},
    ],
)
def test_invalid_quotes_never_rank(change: dict, released: None) -> None:
    c = market(mm.Category.CRYPTO)
    assert mm.rank(c, replace(book(c), **change), FixtureModel(), now=NOW).side is None


def test_three_small_positions_and_daily_reserved_risk(tmp_path: Path, released: None) -> None:
    e = engine(tmp_path / "trial.db")
    cs = [
        market(c, ticker=str(i))
        for i, c in enumerate((mm.Category.CRYPTO, mm.Category.SPORTS, mm.Category.WEATHER))
    ]
    try:
        e.activate()
        e.cycle([(c, book(c)) for c in cs], now=NOW)
        assert e.db.execute("SELECT COUNT(*) FROM mm_intents").fetchone()[0] == 3
        assert e.db.execute("SELECT SUM(risk) FROM mm_intents").fetchone()[0] <= 200
        assert not e.db.execute("SELECT 1 FROM mm_fills").fetchone()
    finally:
        e.close()


def test_correlated_macro_and_duplicate_category_rejected(tmp_path: Path, released: None) -> None:
    e = engine(tmp_path / "trial.db")
    cs = [
        market(c, ticker=str(i))
        for i, c in enumerate((mm.Category.CRYPTO, mm.Category.ECONOMICS, mm.Category.CRYPTO))
    ]
    try:
        e.activate()
        e.cycle([(c, book(c)) for c in cs], now=NOW)
        assert e.db.execute("SELECT COUNT(*) FROM mm_intents").fetchone()[0] == 1
    finally:
        e.close()


def test_cross_category_custom_correlation_blocks_overlap(tmp_path: Path, released: None) -> None:
    e = mm.PaperEngine(
        tmp_path / "trial.db",
        {c: FixtureModel(group="same-storm") for c in (mm.Category.SPORTS, mm.Category.WEATHER)},
    )
    cs = [market(c, ticker=str(i)) for i, c in enumerate((mm.Category.SPORTS, mm.Category.WEATHER))]
    try:
        e.activate()
        e.cycle([(c, book(c)) for c in cs], now=NOW)
        assert e.db.execute("SELECT COUNT(*) FROM mm_intents").fetchone()[0] == 1
    finally:
        e.close()


def test_restart_quote_dedupe_and_kill_retains_inventory(tmp_path: Path, released: None) -> None:
    path = tmp_path / "trial.db"
    c = market(mm.Category.CRYPTO)
    e = engine(path)
    e.activate()
    fill(e, c)
    e.close()
    e = engine(path)
    try:
        e.cycle([(c, book(c, NOW + 1))], now=NOW + 1)
        assert e.db.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 1
        e.shutdown(now=NOW + 2, reason="operator emergency")
        with pytest.raises(mm.SimulationOnlyError, match="Never automatically clear"):
            e.activate()
        e.cycle([(c, book(c, NOW + 3))], now=NOW + 3)
        assert e.db.execute("SELECT status FROM mm_intents").fetchone()[0] == "held"
        assert e.db.execute("SELECT COUNT(*) FROM mm_fills").fetchone()[0] == 1
    finally:
        e.close()


def test_quote_id_collision_kills_without_duplicate_fill(tmp_path: Path, released: None) -> None:
    c = market(mm.Category.CRYPTO)
    e = engine(tmp_path / "trial.db")
    try:
        e.activate()
        e.cycle([(c, book(c))], now=NOW)
        with pytest.raises(RuntimeError, match="Conflicting duplicate"):
            e.cycle([(c, replace(book(c), yes_depth=100))], now=NOW)
        assert e.db.execute("SELECT killed FROM mm_control").fetchone()[0] == 1
        assert not e.db.execute("SELECT 1 FROM mm_fills").fetchone()
    finally:
        e.close()


def test_model_revocation_and_tampered_cash_fail_closed(tmp_path: Path, released: None) -> None:
    path = tmp_path / "trial.db"
    c = market(mm.Category.CRYPTO)
    e = engine(path)
    e.activate()
    e.cycle([(c, book(c))], now=NOW)
    e.models = {}
    e.cycle([(c, book(c, NOW + 1))], now=NOW + 1)
    assert e.db.execute("SELECT status FROM mm_intents").fetchone()[0] == "cancelled"
    e.db.execute("UPDATE mm_account SET cash=9999")
    e.close()
    with pytest.raises(ValueError, match="reconciliation"):
        engine(path)


def test_stale_pending_signal_does_not_fill(tmp_path: Path, released: None) -> None:
    c = market(mm.Category.CRYPTO)
    e = engine(tmp_path / "trial.db")
    try:
        e.activate()
        e.cycle([(c, book(c))], now=NOW)
        e.cycle([(c, book(c, NOW + 6))], now=NOW + 6)
        assert not e.db.execute("SELECT 1 FROM mm_fills").fetchone()
        assert e.db.execute("SELECT status FROM mm_intents").fetchone()[0] == "cancelled"
    finally:
        e.close()


def test_original_account_and_empty_dashboard_are_not_reinitialized(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="original ledger"):
        mm.PaperEngine(tmp_path / "paper.db")
    path = tmp_path / "absent.db"
    data = snapshot(path, now=NOW)
    assert not path.exists() and data["bankroll_cents"] is None
    assert all(c["brier"] is None for c in data["categories"].values())
    assert "Unknown" in render(data)


@pytest.mark.asyncio
async def test_runner_refuses_before_io(tmp_path: Path) -> None:
    e = mm.PaperEngine(tmp_path / "trial.db")
    try:
        with pytest.raises(mm.SimulationOnlyError, match="will not access a feed"):
            await run(e, seconds=10)
        assert not e.db.execute("SELECT 1 FROM sqlite_master WHERE name='mm_reads'").fetchone()
    finally:
        e.close()


@pytest.mark.asyncio
async def test_discovery_requires_actual_quote_event_timestamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kalshi_bot import multi_feed

    monkeypatch.setattr(multi_feed.time, "time", lambda: NOW)
    c = market(mm.Category.WEATHER)
    e = mm.PaperEngine(tmp_path / "trial.db")
    try:
        feed = MultiFeed(e)
        base = "https://demo-api.kalshi.co/trade-api/v2"
        with respx.mock(assert_all_called=True) as api:
            api.get(base + "/markets").respond(200, json={"markets": [c.raw], "cursor": ""})
            api.get(base + "/events/" + c.event).respond(
                200, json={"event": {"event_ticker": c.event, "series_ticker": "TEST"}}
            )
            api.get(base + "/series/TEST").respond(200, json={"series": c.series})
            api.get(base + "/markets/" + c.ticker).respond(200, json={"market": c.raw})
            route = api.get(base + "/markets/" + c.ticker + "/orderbook").respond(
                200,
                json={
                    "orderbook_fp": {
                        "yes_dollars": [["0.40", "12"]],
                        "no_dollars": [["0.58", "12"]],
                    }
                },
            )
            async with KalshiClient("https://demo-api.kalshi.co") as client:
                assert await feed.scan(client) == []
                assert "timestamp" in e.db.execute("SELECT reason FROM mm_candidates").fetchone()[0]
                route.respond(
                    200,
                    json={
                        "quote_timestamp": NOW,
                        "orderbook_fp": {
                            "yes_dollars": [["0.40", "12"]],
                            "no_dollars": [["0.58", "12"]],
                        },
                    },
                )
                assert len(await feed.scan(client)) == 1
        assert e.db.execute("SELECT COUNT(*) FROM mm_reads").fetchone()[0] == 10
        assert e.db.execute("SELECT COUNT(*) FROM mm_orders").fetchone()[0] == 0
    finally:
        e.close()
