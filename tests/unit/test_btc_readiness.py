"""Readiness changes never fetch live data or fit on observed test labels."""

from __future__ import annotations

import copy
import json
import sqlite3
from dataclasses import asdict
from pathlib import Path

import pytest
import respx

from kalshi_bot.btc_collect import Collector, current_candidates, quote_timestamp
from kalshi_bot.btc_observe import book_fields, ledger_state
from kalshi_bot.btc_rules import verify_market
from kalshi_bot.btc_validate import executable_edges
from kalshi_bot.exchange.client import KalshiClient
from tests.unit.test_btc import BOOK, MARKET_DATA, NOW, SERIES_DATA
from tests.unit.test_btc_collect import BASE, account, freeze, headers

__all__ = ["account"]


def executable_payload() -> dict:
    return {
        **book_fields(BOOK),
        **quote_timestamp({"quote_ts": NOW - 1}, NOW),
        "yes_probability": 0.7,
        "reference_valid": True,
        **{
            key + suffix: value
            for key in ("book", "reference", "metadata")
            for suffix, value in (
                ("_received_at", NOW),
                ("_http_age_s", 0.5),
                ("_latency_ms", 20),
            )
        },
    }


def test_strict_edges_use_raw_depth_conservative_costs_and_actual_timestamps() -> None:
    payload = executable_payload()
    result = executable_edges(payload, NOW)
    assert result["strict_execution_eligible"] is True
    assert result["strict_yes_net_edge"] == pytest.approx(0.7 - payload["yes_ask"] - 0.04)
    assert result["strict_no_net_edge"] == pytest.approx(0.3 - payload["no_ask"] - 0.04)
    for changes in (
        {"quote_timestamp_status": "unavailable"},
        {"quote_timestamp": NOW - 6},
        {"quote_timestamp_field": "root.updated_time"},
        {"quote_timestamp_raw": NOW + 1},
        {"quote_timestamp_raw": True},
        {"book_received_at": NOW - 6},
        {"metadata_http_age_s": float("nan")},
        {"reference_latency_ms": 2001},
        {"reference_valid": False},
        {"yes_probability": True},
        {"yes_ask": 0.01},
    ):
        r = executable_edges({**payload, **changes}, NOW)
        assert not r["strict_execution_eligible"], changes
        assert r["strict_yes_net_edge"] is r["strict_no_net_edge"] is None


def test_fractional_one_sided_liquidity_is_never_rounded_up() -> None:
    payload = executable_payload()
    payload.update(
        book_fields({"orderbook_fp": {"yes_dollars": [], "no_dollars": [[".60", ".99"]]}})
    )
    assert not executable_edges(payload, NOW)["strict_execution_eligible"]
    payload.update(
        book_fields({"orderbook_fp": {"yes_dollars": [], "no_dollars": [[".60", "1.25"]]}})
    )
    r = executable_edges(payload, NOW)
    assert r["strict_execution_eligible"]
    assert r["strict_yes_net_edge"] == pytest.approx(0.26)
    assert r["strict_no_net_edge"] is None


def test_current_candidates_filtered_before_request_cap() -> None:
    future = {**MARKET_DATA, "open_time": "2099-01-01T00:00:00Z"}
    candidates = {f"UNRELATED-{i}": future for i in range(20)}
    pending = copy.deepcopy(MARKET_DATA)
    pending.pop("floor_strike")
    candidates[pending["ticker"]] = pending
    assert current_candidates(candidates, NOW) == [(pending["ticker"], pending)]
    assert current_candidates(candidates, NOW + 301) == []


@pytest.mark.asyncio
async def test_cached_rollover_candidate_refreshed_without_relisting_or_strike_guess(
    tmp_path: Path, account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch)
    c = Collector(tmp_path / "rollover.db", account)
    before = ledger_state(account)
    pending = copy.deepcopy(MARKET_DATA)
    pending.pop("floor_strike")
    c.candidates[pending["ticker"]] = pending
    c.next_listing = 200
    with respx.mock(assert_all_called=True) as api:
        api.get(BASE + "/series/KXBTC15M").respond(
            200, json={"series": SERIES_DATA}, headers=headers()
        )
        detail = api.get(BASE + "/markets/" + pending["ticker"]).respond(
            200, json={"market": pending}, headers=headers()
        )
        async with KalshiClient("https://demo-api.kalshi.co") as client:
            await c.discover(client._client)
            assert not c.active
            detail.respond(200, json={"market": MARKET_DATA}, headers=headers())
            c.next_discovery = 0
            await c.discover(client._client)
            assert c.active[0].strike == MARKET_DATA["floor_strike"]
        assert detail.call_count == 2
    assert ledger_state(account) == before
    c.close()


@pytest.mark.asyncio
async def test_invalid_intervals_cannot_consume_detail_refresh_budget(
    tmp_path: Path, account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch)
    c = Collector(tmp_path / "detail-budget.db", account)
    pending = copy.deepcopy(MARKET_DATA)
    pending.pop("floor_strike")
    invalid = [
        {
            **pending,
            "ticker": f"KXBTC15M-000{i}-15",
            "event_ticker": f"KXBTC15M-000{i}",
            # Currently-open looking, but not a verified 15-minute interval.
            "close_time": "2026-11-11T15:30:00Z",
        }
        for i in range(12)
    ]
    with respx.mock(assert_all_called=True) as api:
        api.get(BASE + "/series/KXBTC15M").respond(
            200, json={"series": SERIES_DATA}, headers=headers()
        )
        api.get(BASE + "/markets").respond(
            200, json={"markets": [*invalid, pending], "cursor": ""}, headers=headers()
        )
        detail = api.get(BASE + "/markets/" + pending["ticker"]).respond(
            200, json={"market": MARKET_DATA}, headers=headers()
        )
        async with KalshiClient("https://demo-api.kalshi.co") as client:
            await c.discover(client._client)
        assert detail.call_count == 1
        assert len(c.active) == 1 and c.active[0].ticker == pending["ticker"]
    c.close()


def test_stale_quote_clears_all_actionable_diagnostics(
    tmp_path: Path, account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch)
    c = Collector(tmp_path / "stale.db", account)
    c.slot = 0
    c.current_quotes[MARKET_DATA["ticker"]] = quote_timestamp({"quote_ts": NOW - 10}, NOW)
    p = {
        **book_fields(BOOK),
        "reference_valid": True,
        "errors": [],
        "yes_net_edge": 0.3,
        "minimum_haircut_yes_edge": 0.25,
        "paper_quote_eligible": True,
    }
    c.record_observation(NOW, MARKET_DATA["ticker"], True, False, False, p)
    raw = c.db.execute("SELECT valid,stale,payload FROM observations").fetchone()
    saved = json.loads(raw[2])
    assert raw[:2] == (0, 1)
    assert saved["yes_net_edge"] is saved["minimum_haircut_yes_edge"] is None
    assert not saved["paper_quote_eligible"]
    c.close()


def test_online_snapshot_restores_ledger_guard_deadline_and_unique_slots(
    tmp_path: Path, account: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    freeze(monkeypatch)
    source, restored = tmp_path / "source.db", tmp_path / "restored.db"
    c = Collector(source, account)
    c.slot = 0
    c.record_observation(NOW, None, False, True, False, {})
    c.db.execute("INSERT INTO slots VALUES(0,?,NULL,0,'started')", (NOW,))
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    c.db.execute(
        "INSERT INTO markets VALUES(?,?,?,NULL,NULL,NULL)",
        (market.ticker, NOW, json.dumps(asdict(market))),
    )
    c.db.commit()
    original_deadline = c.manifest["deadline"]
    with (
        sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as src,
        sqlite3.connect(restored) as dst,
    ):
        src.backup(dst)
    c.close()
    before = ledger_state(account)
    resumed = Collector(restored, account, resume=True)
    assert resumed.manifest["deadline"] == original_deadline
    assert resumed.db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert resumed.db.execute("SELECT state FROM slots").fetchone() == ("interrupted",)
    resumed.slot = 0
    resumed.record_observation(NOW, None, False, True, False, {})
    assert resumed.db.execute("SELECT count(*) FROM observations").fetchone() == (1,)
    assert ledger_state(account) == before
    resumed.close()
