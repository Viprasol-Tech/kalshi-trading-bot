"""Offline preregistration and metadata discovery; no empirical model claims."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import respx
from typer.testing import CliRunner

from kalshi_bot.btc_collect import quote_timestamp
from kalshi_bot.btc_validation_manifest import (
    build_manifest,
    schedule_manifest,
    verify_manifest,
)
from kalshi_bot.cli import app
from kalshi_bot.exchange.client import KalshiClient, public_read_path
from kalshi_bot.multi_dashboard import render, snapshot
from kalshi_bot.multi_feed import MultiFeed
from kalshi_bot.multi_market import Category, Contract, PaperEngine, digest
from kalshi_bot.paper import PaperBroker
from tests.unit.test_multi_market import NOW, market


@pytest.fixture
def ledger(tmp_path: Path) -> Path:
    path = tmp_path / "paper.db"
    PaperBroker(path).close()
    return path


def test_manifest_captures_current_fingerprints_without_runtime_creation(ledger: Path) -> None:
    before = ledger.read_bytes()
    m = build_manifest(ledger)
    verify_manifest(m, ledger)
    assert ledger.read_bytes() == before
    assert m["protocol"] is None
    assert m["sampling"]["untouched_test"] == 200
    assert m["risk_policy"]["open_positions"] == 3
    assert all(not c["model_released"] for c in m["categories"].values())
    assert not (ledger.parent / "campaign.db").exists()
    assert not (ledger.parent / "multi-paper.db").exists()
    assert m["manifest_sha256"] == digest({k: v for k, v in m.items() if k != "manifest_sha256"})


@pytest.mark.parametrize(
    "field",
    [
        "collection_enabled",
        "paper_entries_enabled",
        "real_orders_enabled",
        "production_credentials_allowed",
        "hosting_approved",
        "runtime_release_ready",
    ],
)
def test_manifest_cannot_self_authorize_even_with_rehashed_payload(
    ledger: Path, field: str
) -> None:
    m = build_manifest(ledger)
    m[field] = True
    m["manifest_sha256"] = digest({k: v for k, v in m.items() if k != "manifest_sha256"})
    with pytest.raises(ValueError, match="changed"):
        verify_manifest(m, ledger)


@pytest.mark.parametrize("target", ["model_sha256", "source_bundle_sha256", "ledger_sha256"])
def test_stale_fingerprint_rejected(ledger: Path, target: str) -> None:
    m = build_manifest(ledger)
    m[target] = "a" * 64
    with pytest.raises(ValueError):
        verify_manifest(m, ledger)


def test_any_source_change_invalidates_review(
    ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kalshi_bot import btc_validation_manifest as manifest_module

    m = build_manifest(ledger)
    changed = dict(m["source_files"])
    changed["multi_feed.py"] = "a" * 64
    monkeypatch.setattr(manifest_module, "source_files", lambda: changed)
    with pytest.raises(ValueError, match="stale"):
        verify_manifest(m, ledger)


def test_readonly_manifest_cli(ledger: Path) -> None:
    import json

    response = CliRunner().invoke(app, ["btc-validation-manifest", "--ledger", str(ledger)])
    assert response.exit_code == 0
    assert json.loads(response.stdout)["status"] == "DRAFT_NOT_AUTHORIZATION"


def test_missing_ledger_never_reinitialized(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(ValueError):
        build_manifest(path)
    assert not path.exists()


def test_non_pristine_original_account_is_not_claimed_untouched(ledger: Path) -> None:
    import sqlite3

    db = sqlite3.connect(ledger)
    db.execute("UPDATE account SET cash=4999")
    db.commit()
    db.close()
    with pytest.raises(ValueError, match="untouched"):
        build_manifest(ledger)


def test_schedule_compatible_but_never_approved_and_no_rewrite(ledger: Path) -> None:
    times = {
        "registered_at": NOW,
        "starts_at": NOW + 60,
        "development_ends_at": NOW + 100000,
        "test_starts_at": NOW + 200000,
        "deadline": NOW + 400000,
    }
    original = build_manifest(ledger)
    m = schedule_manifest(original, ledger, **times)
    assert original["protocol"] is None
    verify_manifest(m, ledger)
    assert m["protocol"]["test_minimum"] == 200
    assert not m["protocol"]["entries_enabled"]
    assert not m["protocol"]["collection_approved"]
    with pytest.raises(ValueError, match="already frozen"):
        schedule_manifest(m, ledger, **times)
    changed = copy.deepcopy(m)
    changed["protocol"]["test_minimum"] = 199
    with pytest.raises(ValueError):
        verify_manifest(changed, ledger)


@pytest.mark.parametrize("stamp", [None, True, float("nan"), NOW + 1, NOW - 6])
def test_bad_actual_timestamp_never_gets_receipt_substitute(stamp: object) -> None:
    result = quote_timestamp({"quote_timestamp": stamp, "updated_time": NOW}, NOW)
    assert result["quote_timestamp_status"] != "fresh"


def test_conflicting_timestamp_fields_rejected() -> None:
    r = quote_timestamp({"quote_timestamp": NOW, "orderbook_fp": {"quote_ts": NOW - 1}}, NOW)
    assert r["quote_timestamp_status"] == "invalid_or_stale"
    assert r["quote_timestamp_conflict"]


def test_metadata_and_http_dates_are_not_quote_times() -> None:
    r = quote_timestamp({"updated_time": NOW, "Date": NOW, "timestamp": NOW}, NOW)
    assert r["quote_timestamp_status"] == "unavailable"
    assert r["quote_timestamp"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,expected",
    [
        ({}, True),
        ({"series_ticker": None}, False),
        ({"event_ticker": "WRONG"}, False),
        ({"fee_multiplier_override": 2}, False),
        ({"category": "Politics"}, False),
    ],
)
async def test_provider_metadata_not_ticker_parsing_drives_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: dict, expected: bool
) -> None:
    from kalshi_bot import multi_feed

    monkeypatch.setattr(multi_feed.time, "time", lambda: NOW)
    c = market(Category.WEATHER)
    raw = {**c.raw, "event_ticker": "UNRELATED-EVENT", "ticker": "NONPREFIX-MARKET"}
    event = {"event_ticker": raw["event_ticker"], "series_ticker": "TEST", **mutation}
    e = PaperEngine(tmp_path / "trial.db")
    try:
        feed = MultiFeed(e)
        base = "https://demo-api.kalshi.co/trade-api/v2"
        with respx.mock(assert_all_called=False) as api:
            api.get(base + "/markets").respond(200, json={"markets": [raw], "cursor": ""})
            api.get(base + "/events/UNRELATED-EVENT").respond(200, json={"event": event})
            api.get(base + "/series/TEST").respond(200, json={"series": c.series})
            api.get(base + "/markets/NONPREFIX-MARKET").respond(200, json={"market": raw})
            api.get(base + "/markets/NONPREFIX-MARKET/orderbook").respond(
                200,
                json={
                    "quote_timestamp": NOW,
                    "orderbook_fp": {
                        "yes_dollars": [["0.40", "12"]],
                        "no_dollars": [["0.58", "12"]],
                    },
                },
            )
            async with KalshiClient("https://demo-api.kalshi.co") as client:
                frames = await feed.scan(client)
            assert bool(frames) is expected
            assert not any("/series/UNRELATED" in str(call.request.url) for call in api.calls)
        assert e.db.execute("SELECT COUNT(*) FROM mm_orders").fetchone()[0] == 0
        if expected:
            row = e.db.execute("SELECT * FROM mm_metadata").fetchone()
            assert row["series"] == "TEST" and row["provider_category"] == "weather"
    finally:
        e.close()


def test_only_public_demo_event_reads_added() -> None:
    assert public_read_path("/events/EXAMPLE")
    assert not public_read_path("/portfolio/orders")
    assert not public_read_path("/historical/fills")


def test_dashboard_contains_orders_with_no_execution_control(tmp_path: Path) -> None:
    e = PaperEngine(tmp_path / "trial.db")
    try:
        data = snapshot(tmp_path / "trial.db", now=NOW)
        assert data["orders"] == []
        assert not data["errors"]
        assert "Orders (latest 100)" in render(data)
        assert "No Start Trading button" in render(data)
    finally:
        e.close()


@pytest.mark.parametrize("floor", [None, True, 0, -1, "95000", float("nan")])
def test_btc_missing_or_unverified_floor_never_inferred(floor: object) -> None:
    c = market(Category.CRYPTO)
    raw = {
        **c.raw,
        "ticker": "KXBTC15M-E-M",
        "event_ticker": "KXBTC15M-E",
        "floor_strike": floor,
        "title": "BTC above $95,000",
    }
    with pytest.raises(ValueError, match="floor strike"):
        Contract.verify(raw, {**c.series, "ticker": "KXBTC15M"}, at=NOW, now=NOW)


@pytest.mark.parametrize("status", ["settled", "closed", "determined", "active"])
def test_nonfinalized_labels_cannot_release_money(tmp_path: Path, status: str) -> None:
    c = market(Category.WEATHER)
    e = PaperEngine(tmp_path / "trial.db")
    try:
        with pytest.raises(ValueError, match="finalized"):
            e.settle(c, {**c.raw, "status": status}, received_at=NOW, now=NOW)
        assert e.db.execute("SELECT cash FROM mm_account").fetchone()[0] == 5000
        assert not e.db.execute("SELECT 1 FROM mm_settlements").fetchone()
    finally:
        e.close()


@pytest.mark.asyncio
async def test_unknown_actual_provider_category_retained_not_guessed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kalshi_bot import multi_feed

    monkeypatch.setattr(multi_feed.time, "time", lambda: NOW)
    c = market(Category.WEATHER)
    raw = {**c.raw, "title": "Bitcoin / Sports / Weather"}
    series = {**c.series, "category": "Actual provider new category"}
    e = PaperEngine(tmp_path / "trial.db")
    try:
        feed = MultiFeed(e)
        base = "https://demo-api.kalshi.co/trade-api/v2"
        with respx.mock(assert_all_called=True) as api:
            api.get(base + "/markets").respond(200, json={"markets": [raw], "cursor": ""})
            api.get(base + "/events/" + c.event).respond(
                200, json={"event": {"event_ticker": c.event, "series_ticker": "TEST"}}
            )
            api.get(base + "/series/TEST").respond(200, json={"series": series})
            api.get(base + "/markets/" + c.ticker).respond(200, json={"market": raw})
            async with KalshiClient("https://demo-api.kalshi.co") as client:
                assert not await feed.scan(client)
        assert e.db.execute("SELECT provider_category FROM mm_metadata").fetchone()[0] == (
            "Actual provider new category"
        )
        assert e.db.execute("SELECT category FROM mm_candidates").fetchone()[0] is None
        assert not e.db.execute("SELECT 1 FROM mm_orders").fetchone()
    finally:
        e.close()
