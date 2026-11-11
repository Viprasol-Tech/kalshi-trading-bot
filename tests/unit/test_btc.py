"""Phase 3 fixtures exercise logic, NOT empirical calibration or profitability."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from statistics import NormalDist
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from pydantic import ValidationError
from typer.testing import CliRunner

from kalshi_bot.btc_data import CoinbaseReference, DemoBTCSource, orderbook_quote
from kalshi_bot.btc_model import (
    CalibrationArtifact,
    CalibrationRecord,
    Estimate,
    ReferenceTick,
    calibration_check,
    estimate,
)
from kalshi_bot.btc_rules import SECONDARY, TERMS_URL, utc_timestamp, verify_market
from kalshi_bot.btc_strategy import BTCPaperRunner, select_entry
from kalshi_bot.cli import app
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.exchange.models import Side
from kalshi_bot.paper import PaperBroker, PaperQuote

OPEN = utc_timestamp("2026-11-11T15:00:00Z")
NOW = OPEN + 600
SERIES_DATA = {
    "ticker": "KXBTC15M",
    "frequency": "fifteen_min",
    "contract_terms_url": TERMS_URL,
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "CF Benchmarks", "url": "https://www.cfbenchmarks.com/"}],
}
MARKET_DATA = {
    "ticker": "KXBTC15M-26NOV111015-15",
    "event_ticker": "KXBTC15M-26NOV111015",
    "open_time": "2026-11-11T15:00:00Z",
    "close_time": "2026-11-11T15:15:00Z",
    "expiration_time": "2026-11-18T15:15:00Z",
    "floor_strike": 73473.85,
    "market_type": "binary",
    "status": "active",
    "result": "",
    "strike_type": "greater_or_equal",
    "custom_strike": {"round_digits": "2"},
    "notional_value_dollars": "1.0000",
    "rules_primary": (
        "If the simple average of the sixty seconds of CF Benchmarks' BRTI before "
        "10:15 AM EST on Nov 11, 2026 is at least the simple average of the sixty seconds "
        "of CF Benchmarks' BRTI before 10:00 AM EST on November 11, 2026, "
        "then the market resolves to Yes."
    ),
    "rules_secondary": SECONDARY,
}
BOOK = {"orderbook_fp": {"yes_dollars": [["0.4050", "12.99"]], "no_dollars": [["0.5500", "8.25"]]}}


def history(end: float = NOW, spot: float = 73500, n: int = 121) -> list[ReferenceTick]:
    return [
        ReferenceTick(timestamp=end - (n - 1 - i) * 2, price=spot * (1 + 0.0001 * (i % 2)))
        for i in range(n)
    ]


def paper_quote(now: float = NOW, **changes: object) -> PaperQuote:
    data = {
        "ticker": MARKET_DATA["ticker"],
        "quote_id": str(now),
        "observed_at": now,
        "yes_bid": 49,
        "yes_ask": 50,
        "no_bid": 50,
        "no_ask": 51,
        "yes_bid_size": 20,
        "yes_ask_size": 20,
        "no_bid_size": 20,
        "no_ask_size": 20,
    }
    data.update(changes)
    return PaperQuote.model_validate(data)


def test_rules_use_close_not_administrative_expiration() -> None:
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    assert market.closes_at == OPEN + 900
    assert market.administrative_expiration == OPEN + 900 + 7 * 86400
    assert market.strike == 73473.85


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "closed"},
        {"market_type": "scalar"},
        {"strike_type": "greater"},
        {"floor_strike": None},
        {"floor_strike": float("nan")},
        {"floor_strike": 73473.851},
        {"custom_strike": {"round_digits": "3"}},
        {"open_time": "2026-11-11T14:00:00Z"},
        {"close_time": "2026-11-11T15:30:00Z"},
        {"rules_primary": MARKET_DATA["rules_primary"].replace("at least", "above")},
        {"rules_secondary": SECONDARY.replace("CF Benchmarks", "Coinbase")},
        {"result": "yes"},
    ],
)
def test_uncertain_market_rules_fail_closed(changes: dict[str, object]) -> None:
    raw = {**MARKET_DATA, **changes}
    with pytest.raises((ValueError, TypeError, KeyError)):
        verify_market(raw, SERIES_DATA, now=NOW)


def test_wrong_source_and_stale_market_rejected() -> None:
    with pytest.raises(ValueError):
        verify_market(MARKET_DATA, {**SERIES_DATA, "settlement_sources": []}, now=NOW)
    with pytest.raises(ValueError):
        verify_market(MARKET_DATA, SERIES_DATA, now=OPEN + 900)


def test_fixed_point_book_is_rounded_conservatively() -> None:
    book = orderbook_quote(BOOK, MARKET_DATA["ticker"], NOW)
    assert book.yes_bid == 40
    assert book.no_ask == 60
    assert book.yes_ask == 45
    assert book.yes_bid_size == 12
    assert book.yes_ask_size == 8


def test_subcent_crossed_book_cannot_be_hidden_by_rounding() -> None:
    with pytest.raises(ValueError):
        orderbook_quote(
            {"orderbook_fp": {"yes_dollars": [["0.405", "20"]], "no_dollars": [["0.599", "20"]]}},
            "T",
            NOW,
        )


@pytest.mark.parametrize(
    "args",
    [
        ["btc-paper", "--live"],
        ["btc-paper", "--ticks", "0"],
        ["btc-paper", "--interval", "0"],
    ],
)
def test_btc_cli_rejects_live_and_unbounded_parameters(args: list[str]) -> None:
    assert CliRunner().invoke(app, args).exit_code != 0


@pytest.mark.parametrize(
    "levels",
    [
        [],
        [["0.4", "3.99"]],
        [["NaN", "20"]],
        [["0.4", "-1"]],
        [["1.1", "20"]],
    ],
)
def test_missing_or_invalid_liquidity_rejected(levels: list[list[str]]) -> None:
    body = {"orderbook_fp": {"yes_dollars": levels, "no_dollars": [["0.5", "10"]]}}
    with pytest.raises(ValueError):
        orderbook_quote(body, "T", NOW)


def test_model_direction_and_final_minute_averaging() -> None:
    ticks = history()
    at_money = estimate(ticks, ticks[-1].price, OPEN + 900, now=NOW)
    cheap_strike = estimate(ticks, 73400, OPEN + 900, now=NOW)
    rich_strike = estimate(ticks, 73600, OPEN + 900, now=NOW)
    assert 0.49 < at_money.yes_probability < 0.51
    assert cheap_strike.yes_probability > at_money.yes_probability > rich_strike.yes_probability
    assert at_money.reference_samples == 121


@pytest.mark.parametrize(
    "ticks,close,now",
    [
        (history(n=119), OPEN + 900, NOW),
        (history(), NOW + 59, NOW),
        (history(), OPEN + 900, NOW + 6),
        (history(), OPEN + 900, NOW - 1),
        ([*history()[:-1], history()[-2]], OPEN + 900, NOW),
        (
            [ReferenceTick(timestamp=NOW - 240 + i * 2, price=73500) for i in range(121)],
            OPEN + 900,
            NOW,
        ),
    ],
)
def test_bad_reference_or_horizon_skips(
    ticks: list[ReferenceTick],
    close: float,
    now: float,
) -> None:
    with pytest.raises(ValueError):
        estimate(ticks, 73500, close, now=now)


def test_eight_point_net_edge_after_fees_slippage_and_haircut() -> None:
    forecast = Estimate(0.66, 0.0001, 121, 300)
    entry = select_entry(forecast, paper_quote(), haircut=0.05, available_cash=5000)
    assert entry is not None
    assert entry.side is Side.YES
    assert entry.net_edge == 0.08  # .66 - .05 - (.50 + .01 + .02)
    assert entry.maximum_loss == 53 and entry.count == 1
    assert (
        select_entry(
            Estimate(0.659, 0.0001, 121, 300), paper_quote(), haircut=0.05, available_cash=5000
        )
        is None
    )
    assert select_entry(forecast, paper_quote(), haircut=0.05, available_cash=52) is None
    assert (
        select_entry(forecast, paper_quote(yes_ask_size=3), haircut=0.05, available_cash=5000)
        is None
    )


def test_no_side_probability_and_budget() -> None:
    entry = select_entry(
        Estimate(0.1, 0.0001, 121, 300), paper_quote(), haircut=0.05, available_cash=5000
    )
    assert entry is not None and entry.side is Side.NO
    assert entry.maximum_loss <= 100


def synthetic_calibration_for_unit_tests() -> CalibrationArtifact:
    """Intentionally fake observations for testing gates only. Never written to disk."""
    records = []
    for i in range(200):
        predicted = NOW - (201 - i) * 900
        ticks = history(predicted)
        sd = (
            ticks[-1].price
            * estimate(ticks, 73500, predicted + 300, now=predicted).volatility_per_sqrt_second
            * math.sqrt(260)
        )
        target = 0.96 if i % 2 else 0.04
        strike = ticks[-1].price - NormalDist().inv_cdf(target) * sd + 0.005
        ticker = f"KXBTC15M-UNITTEST{i}-15"
        records.append(
            CalibrationRecord(
                ticker=ticker,
                rules_sha256="a" * 64,
                strike=strike,
                predicted_at=predicted,
                closes_at=predicted + 300,
                resolved_at=predicted + 310,
                yes_outcome=i % 2,
                label_source="https://demo-api.kalshi.co/trade-api/v2/markets/" + ticker,
                ticks=ticks,
            )
        )
    return CalibrationArtifact(
        provenance="observed-demo",
        fitted_before=NOW - 203 * 900,
        evaluated_at=NOW - 1,
        records=records,
    )


def test_calibration_recomputed_and_required() -> None:
    assert not calibration_check(None, now=NOW, horizon=300, yes_probability=0.9)["passed"]
    artifact = synthetic_calibration_for_unit_tests()
    good = calibration_check(artifact, now=NOW, horizon=300, yes_probability=0.9)
    assert good["passed"] and good["sample_size"] == 200
    assert good["brier"] < 0.01
    # Unobserved horizon/probability regions are not covered by overall good scores.
    assert not calibration_check(artifact, now=NOW, horizon=600, yes_probability=0.9)["passed"]
    assert not calibration_check(artifact, now=NOW, horizon=300, yes_probability=0.5)["passed"]
    assert not calibration_check(artifact, now=NOW + 8 * 86400, horizon=300, yes_probability=0.9)[
        "passed"
    ]
    bad = artifact.model_dump()
    bad["records"][0]["yes_outcome"] = 1
    bad["records"][1]["ticker"] = bad["records"][0]["ticker"]
    assert not calibration_check(
        CalibrationArtifact.model_validate(bad), now=NOW, horizon=300, yes_probability=0.9
    )["passed"]


@pytest.mark.parametrize(
    "changes",
    [
        {"model_id": "other"},
        {"source": "CF-BRTI"},
        {"provenance": "synthetic"},
    ],
)
def test_wrong_calibration_provenance_rejected(changes: dict[str, str]) -> None:
    data = {
        "provenance": "observed-demo",
        "fitted_before": NOW - 10,
        "evaluated_at": NOW - 1,
        "records": [],
        **changes,
    }
    with pytest.raises(ValidationError):
        CalibrationArtifact.model_validate(data)


def response(body: object, at: float = NOW) -> httpx.Response:
    return httpx.Response(
        200, json=body, headers={"Date": format_datetime(datetime.fromtimestamp(at, UTC))}
    )


@pytest.mark.asyncio
async def test_discovery_paginates_and_does_not_trust_status_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.time", lambda: NOW)
    with respx.mock:
        respx.get("https://demo-api.kalshi.co/trade-api/v2/series/KXBTC15M").mock(
            return_value=response({"series": SERIES_DATA})
        )
        route = respx.get("https://demo-api.kalshi.co/trade-api/v2/markets").mock(
            side_effect=[
                response({"markets": [{**MARKET_DATA, "status": "closed"}], "cursor": "next"}),
                response({"markets": [MARKET_DATA], "cursor": ""}),
            ]
        )
        async with KalshiClient("https://demo-api.kalshi.co/trade-api/v2") as client:
            markets, skips = await DemoBTCSource(client).discover()
        assert len(markets) == 1 and len(skips) == 1 and route.call_count == 2
        assert route.calls[1].request.url.params["cursor"] == "next"


@pytest.mark.asyncio
async def test_reference_timestamp_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.time", lambda: NOW)
    reference = CoinbaseReference()
    try:
        with respx.mock:
            route = respx.get(reference.URL).mock(
                return_value=response(
                    {
                        "time": datetime.fromtimestamp(NOW, UTC).isoformat(),
                        "price": "73500",
                        "bid": "73499",
                        "ask": "73501",
                    }
                )
            )
            assert (await reference.tick()).price == 73500
            route.mock(
                return_value=response(
                    {
                        "time": datetime.fromtimestamp(NOW - 10, UTC).isoformat(),
                        "price": "73500",
                        "bid": "73499",
                        "ask": "73501",
                    }
                )
            )
            with pytest.raises(ValueError):
                await reference.tick()
    finally:
        await reference.close()


@pytest.mark.asyncio
async def test_runner_without_calibration_never_submits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.time", lambda: NOW)
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    source, reference = AsyncMock(), AsyncMock()
    source.discover.return_value = ([market], [])
    source.refresh.return_value = (market, paper_quote())
    reference.tick.return_value = history()[-1]
    broker = PaperBroker(":memory:")
    try:
        runner = BTCPaperRunner(broker, source, reference)
        for tick in history():
            runner._remember(tick)
        report = await runner.run(ticks=1)
        assert report["calibration_loaded"] is False
        assert report["entry_attempts"] == 0
        assert broker.rows("orders") == []
        assert broker.account()["cash"] == 5000
        assert any("calibration" in s["reason"] for s in report["recent_skips"])
    finally:
        broker.close()


def test_one_entry_budget_persists_across_repeated_signals() -> None:
    broker = PaperBroker(":memory:")
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    choice = select_entry(
        Estimate(0.9, 0.0001, 121, 300), paper_quote(), haircut=0.05, available_cash=5000
    )
    assert choice is not None
    try:
        runner = BTCPaperRunner(broker, AsyncMock(), AsyncMock())
        runner._entry(market, Estimate(0.9, 0.0001, 121, 300), choice, NOW)
        broker.cancel_market(market.ticker)
        runner._entry(market, Estimate(0.9, 0.0001, 121, 300), choice, NOW + 1)
        assert len(broker.rows("orders")) == 1
        assert runner.report()["entry_attempts"] == 1
    finally:
        broker.close()


@pytest.mark.asyncio
async def test_runner_calibrated_path_fills_only_on_a_later_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [NOW]
    monkeypatch.setattr("time.time", lambda: clock[0])
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    source, reference = AsyncMock(), AsyncMock()
    source.discover.return_value = ([market], [])
    source.refresh.side_effect = [
        (market, paper_quote(NOW)),
        (market, paper_quote(NOW + 2, yes_bid=48, yes_ask=49, no_bid=51, no_ask=52)),
    ]
    initial = history(spot=73700)
    reference.tick.side_effect = [
        initial[-1],
        ReferenceTick(timestamp=NOW + 2, price=73700 * 1.0001),
    ]
    broker = PaperBroker(":memory:")
    try:
        runner = BTCPaperRunner(broker, source, reference, synthetic_calibration_for_unit_tests())
        for tick in initial:
            runner._remember(tick)
        await runner.step()
        assert len(broker.rows("orders")) == 1
        assert broker.rows("fills") == []
        assert broker.account()["reserved_cash"] == 53
        assert runner.db.execute("SELECT COUNT(*) FROM btc_forecasts").fetchone()[0] == 1
        clock[0] += 2
        await runner.step()
        assert len(broker.rows("fills")) == 1
        assert len(broker.rows("orders")) == 1
        assert broker.account()["cash"] == 4948
        assert broker.account()["fees"] == 2
        # Only the confirmed Kalshi result can settle, never the Coinbase price.
        source.result.return_value = (
            100,
            "https://demo-api.kalshi.co/trade-api/v2/markets/" + market.ticker,
        )
        await runner._settle_known(OPEN + 901)
        assert broker.account()["cash"] == 5048
        assert runner.report()["settled_filled_markets"] == 1
        assert runner.report()["win_rate"] == 1.0
        assert runner.report()["forecast_resolved_market_sample"] == 1
        # Simulate a crash after the atomic ledger settlement but before model labels.
        runner.db.execute("UPDATE btc_entries SET outcome=NULL")
        runner.db.execute("UPDATE btc_forecasts SET outcome=NULL")
        await runner._settle_known(OPEN + 902)
        assert runner.report()["forecast_resolved_market_sample"] == 1
        assert broker.account()["cash"] == 5048  # no duplicate payout
    finally:
        broker.close()


@pytest.mark.asyncio
async def test_missing_gate_cancels_preexisting_entry_before_fill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [NOW]
    monkeypatch.setattr("time.time", lambda: clock[0])
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    source, reference = AsyncMock(), AsyncMock()
    source.discover.return_value = ([market], [])
    source.refresh.side_effect = [(market, paper_quote(NOW)), (market, paper_quote(NOW + 2))]
    reference.tick.side_effect = [
        history(spot=73700)[-1],
        ReferenceTick(timestamp=NOW + 2, price=73700 * 1.0001),
    ]
    broker = PaperBroker(":memory:")
    try:
        runner = BTCPaperRunner(broker, source, reference, synthetic_calibration_for_unit_tests())
        for tick in history(spot=73700):
            runner._remember(tick)
        await runner.step()
        assert broker.pending()
        runner.calibration = None
        clock[0] += 2
        await runner.step()
        assert not broker.pending()
        assert broker.rows("fills") == []
        assert broker.account()["cash"] == 5000
    finally:
        broker.close()


@pytest.mark.asyncio
async def test_runtime_error_cancels_pending_and_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.time", lambda: NOW)
    broker = PaperBroker(":memory:")
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    source = AsyncMock()
    source.discover.side_effect = RuntimeError("injected unexpected condition")
    try:
        runner = BTCPaperRunner(broker, source, AsyncMock())
        runner.db.execute(
            "INSERT INTO btc_markets VALUES(?,?)", (market.ticker, json.dumps(asdict(market)))
        )
        choice = select_entry(
            Estimate(0.9, 0.0001, 121, 300), paper_quote(), haircut=0.05, available_cash=5000
        )
        assert choice
        runner._entry(market, Estimate(0.9, 0.0001, 121, 300), choice, NOW)
        with pytest.raises(RuntimeError):
            await runner.run()
        assert not broker.pending()
        assert broker.rows("fills") == []
    finally:
        broker.close()


def test_entry_and_feature_history_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "btc.db"
    broker = PaperBroker(path)
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    runner = BTCPaperRunner(broker, AsyncMock(), AsyncMock())
    runner._remember(history()[-1])
    choice = select_entry(
        Estimate(0.9, 0.0001, 121, 300), paper_quote(), haircut=0.05, available_cash=5000
    )
    assert choice
    runner._entry(market, Estimate(0.9, 0.0001, 121, 300), choice, NOW)
    broker.close()
    broker = PaperBroker(path)
    try:
        runner = BTCPaperRunner(broker, AsyncMock(), AsyncMock())
        runner._entry(market, Estimate(0.9, 0.0001, 121, 300), choice, NOW + 1)
        assert len(broker.rows("orders")) == 1
        assert runner.report()["reference_samples"] == 1
    finally:
        broker.close()


@pytest.mark.asyncio
async def test_finalized_result_revalidates_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.time", lambda: OPEN + 901)
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    with respx.mock:
        url = "https://demo-api.kalshi.co/trade-api/v2/markets/" + market.ticker
        route = respx.get(url).mock(
            return_value=response(
                {"market": {**MARKET_DATA, "status": "finalized", "result": "yes"}}, OPEN + 901
            )
        )
        respx.get("https://demo-api.kalshi.co/trade-api/v2/series/KXBTC15M").mock(
            return_value=response({"series": SERIES_DATA}, OPEN + 901)
        )
        async with KalshiClient("https://demo-api.kalshi.co/trade-api/v2") as client:
            source = DemoBTCSource(client)
            assert await source.result(market) == (100, url)
            route.mock(
                return_value=response(
                    {
                        "market": {
                            **MARKET_DATA,
                            "status": "finalized",
                            "result": "yes",
                            "floor_strike": 70000,
                        }
                    },
                    OPEN + 901,
                )
            )
            with pytest.raises(ValueError):
                await source.result(market)


@pytest.mark.asyncio
async def test_stale_http_response_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.time", lambda: NOW)
    with respx.mock:
        respx.get("https://demo-api.kalshi.co/trade-api/v2/series/KXBTC15M").mock(
            return_value=response({"series": SERIES_DATA}, NOW - 60)
        )
        async with KalshiClient("https://demo-api.kalshi.co/trade-api/v2") as client:
            with pytest.raises(ValueError):
                await DemoBTCSource(client).discover()
