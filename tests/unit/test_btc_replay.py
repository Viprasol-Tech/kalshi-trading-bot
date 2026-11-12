"""Synthetic fixtures validate replay mechanics only, never empirical performance."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from kalshi_bot.btc_history import history_audit
from kalshi_bot.btc_replay import (
    ReplayFrame,
    ReplayOutcome,
    ReplaySource,
    append_observation,
    load_observations,
    replay,
)
from kalshi_bot.btc_rules import verify_market
from kalshi_bot.cli import app
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.safety import SimulationOnlyError
from tests.unit.test_btc import (
    MARKET_DATA,
    NOW,
    OPEN,
    SERIES_DATA,
    history,
    paper_quote,
    response,
    synthetic_calibration_for_unit_tests,
)

pytestmark = pytest.mark.usefixtures("offline_execution_fixture")


def frame(at: float = NOW, spot: float = 73700) -> dict[str, Any]:
    return {
        "kind": "frame",
        "observed_at": at,
        "metadata_received_at": at,
        "series_received_at": at,
        "reference_received_at": at,
        "reference": history(at, spot=spot)[-1].model_dump(),
        "quote": paper_quote(at).model_dump(),
        "market": deepcopy(MARKET_DATA),
        "series": deepcopy(SERIES_DATA),
    }


def outcome(at: float = OPEN + 901) -> dict[str, Any]:
    return {
        "kind": "outcome",
        "observed_at": at,
        "market": {
            **MARKET_DATA,
            "status": "finalized",
            "result": "yes",
            "settlement_ts": "2026-11-11T15:15:01Z",
            "settlement_value_dollars": "1.0000",
        },
        "series": SERIES_DATA,
    }


@pytest.mark.parametrize(
    "field",
    [
        "metadata_received_at",
        "series_received_at",
        "reference_received_at",
    ],
)
def test_snapshot_cannot_contain_future_data(field: str) -> None:
    data = frame()
    data[field] = NOW + 1
    with pytest.raises(ValidationError):
        ReplayFrame.model_validate({**data, "provenance": "observed-demo"})


def test_outcome_cannot_be_available_before_settlement() -> None:
    with pytest.raises(ValidationError):
        ReplayOutcome.model_validate(
            {
                **outcome(OPEN + 900.5),
                "provenance": "observed-demo",
            }
        )


@pytest.mark.asyncio
async def test_outcome_is_not_visible_until_its_observed_time(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    append_observation(path, frame())
    append_observation(path, outcome())
    observations, _ = load_observations(path)
    source = ReplaySource(observations)
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    source.now = NOW
    assert await source.result(market) is None
    source.now = OPEN + 901
    assert (await source.result(market))[0] == 100


@pytest.mark.asyncio
async def test_no_calibration_means_no_replay_entries(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    for tick in history(spot=73700):
        data = frame(tick.timestamp)
        data["reference"] = tick.model_dump()
        append_observation(path, data)
    append_observation(path, outcome())
    report, events = await replay(path)
    assert report["account"]["cash"] == 5000
    assert report["entry_attempts"] == 0
    assert report["fill_events"] == 0
    assert report["forecast_resolved_market_sample"] == 1
    assert report["promotion_allowed"] is False
    assert any(e["kind"] == "btc_skipped" for e in events)


@pytest.mark.asyncio
async def test_policy_replay_preserves_later_quote_fills_and_full_bankroll(
    tmp_path: Path,
) -> None:
    path = tmp_path / "observations.jsonl"
    ticks = history(spot=73700)
    for tick in ticks:
        data = frame(tick.timestamp)
        data["reference"] = tick.model_dump()
        append_observation(path, data)
    next_frame = frame(NOW + 2)
    next_frame["reference"]["price"] = 73700 * 1.0001
    append_observation(path, next_frame)
    append_observation(path, outcome())
    report, events = await replay(path, synthetic_calibration_for_unit_tests())
    assert report["entry_attempts"] == 1
    assert report["fill_events"] == 1
    assert report["account"]["cash"] == 5047  # 100 - 51 premium - 2 fee
    assert report["account"]["initial_cash"] == 5000
    assert report["settled_filled_markets"] == 1
    assert report["win_rate_95pct_wilson_interval"][0] < 0.21
    assert report["persistent_bankroll_modified"] is False
    assert report["profitability_established"] is False  # fixture, not evidence
    fills = [e for e in events if e["kind"] == "fill"]
    assert len(fills) == 1


@pytest.mark.asyncio
async def test_one_second_latency_does_not_fill_on_half_second_quote(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    for tick in history(spot=73700):
        data = frame(tick.timestamp)
        data["reference"] = tick.model_dump()
        append_observation(path, data)
    append_observation(path, frame(NOW + 0.5))
    report, _ = await replay(path, synthetic_calibration_for_unit_tests())
    assert report["entry_attempts"] == 1
    assert report["fill_events"] == 0
    assert report["account"]["reserved_cash"] == 0


def test_archive_order_and_conflicting_labels_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    append_observation(path, frame())
    append_observation(path, frame(NOW - 1))
    with pytest.raises(ValueError, match="observation order"):
        load_observations(path)
    path.unlink()
    append_observation(path, frame())
    append_observation(path, outcome())
    changed = outcome(OPEN + 902)
    changed["market"] = {
        **changed["market"],
        "result": "no",
        "settlement_value_dollars": "0.0000",
    }
    append_observation(path, changed)
    with pytest.raises(ValueError, match="Conflicting outcomes"):
        load_observations(path)


def test_candles_cannot_be_used_as_snapshot_input(tmp_path: Path) -> None:
    path = tmp_path / "candles.jsonl"
    path.write_text(json.dumps({"end_period_ts": NOW, "volume_fp": "100.00"}) + "\n")
    with pytest.raises(ValueError):
        load_observations(path)


@pytest.mark.asyncio
async def test_replay_cannot_use_future_evaluated_calibration(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    for tick in history(spot=73700):
        data = frame(tick.timestamp)
        data["reference"] = tick.model_dump()
        append_observation(path, data)
    artifact = synthetic_calibration_for_unit_tests().model_copy(
        update={"evaluated_at": NOW + 1},
    )
    report, _ = await replay(path, artifact)
    assert report["entry_attempts"] == 0


@pytest.mark.asyncio
async def test_history_audit_reports_null_not_fake_zero_pnl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("time.time", lambda: OPEN + 902)
    source = AsyncMock()
    finalized = outcome()["market"]

    async def get(path: str, params: Any = None) -> tuple[dict[str, Any], float]:
        if path.startswith("/series/") and path.endswith("/candlesticks"):
            return {"ticker": MARKET_DATA["ticker"], "candlesticks": []}, OPEN + 902
        if path == "/series/KXBTC15M":
            return {"series": SERIES_DATA}, OPEN + 902
        if path == "/historical/cutoff":
            return {"market_settled_ts": "2026-09-12T00:00:00Z"}, OPEN + 902
        return {"markets": [finalized], "cursor": ""}, OPEN + 902

    source.get.side_effect = get
    import respx

    with respx.mock:
        respx.get("https://api.exchange.coinbase.com/products/BTC-USD/candles").mock(
            return_value=response([], OPEN + 902)
        )
        report = await history_audit(source, limit=1)
    assert report["verified_finalized_labels"] == 1
    assert report["historical_pnl_cents"] is None
    assert report["historical_win_rate"] is None
    assert report["execution_eligible_market_sample"] == 0
    assert report["entries_enabled"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/historical/orders",
        "/historical/fills",
        "/historical/positions",
    ],
)
async def test_private_historical_data_remains_blocked(path: str) -> None:
    async with KalshiClient("https://demo-api.kalshi.co/trade-api/v2") as client:
        with pytest.raises(SimulationOnlyError):
            await client._request("GET", path)
        with pytest.raises(SimulationOnlyError):
            await client._client.get(path)


def test_cli_outputs_blocked_report_instead_of_fake_metrics(tmp_path: Path) -> None:
    source = tmp_path / "candles.jsonl"
    source.write_text('{"end_period_ts":1}\n')
    output = tmp_path / "report.json"
    result = CliRunner().invoke(
        app,
        [
            "btc-replay",
            str(source),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 2
    report = json.loads(output.read_text())
    assert report["status"] == "blocked_invalid_or_inadequate_archive"
    assert report["historical_pnl_cents"] is None


def test_balance_command_does_not_request_credentials() -> None:
    result = CliRunner().invoke(app, ["balance"])
    assert result.exit_code == 1
    assert "paper-status" in result.output
    assert "do not add keys" in result.output


@pytest.mark.asyncio
async def test_reference_and_rejection_events_preserve_actual_cancellations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "observations.jsonl"
    for tick in history(spot=73700):
        append_observation(
            path,
            {
                "kind": "reference",
                "observed_at": tick.timestamp,
                "reference": tick.model_dump(),
            },
        )
    append_observation(path, frame())
    append_observation(
        path,
        {
            "kind": "skip",
            "observed_at": NOW + 0.5,
            "ticker": MARKET_DATA["ticker"],
            "reason": "insufficient recorded depth",
        },
    )
    append_observation(path, frame(NOW + 2))
    report, _ = await replay(path, synthetic_calibration_for_unit_tests())
    assert report["entry_attempts"] == 1
    assert report["fill_events"] == 0  # must not fill the order cancelled during the gap
    assert report["account"]["cash"] == 5000
    assert report["reference_samples"] >= 121
