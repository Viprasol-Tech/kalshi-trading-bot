"""Offline restart, rollover, circuit-breaker, and six-hour-boundary tests."""

from __future__ import annotations

import copy
import json
import math
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from kalshi_bot import btc_collect
from kalshi_bot.btc_collect import Collector, classify_book, quote_timestamp
from kalshi_bot.btc_model import MODEL_ID
from kalshi_bot.btc_observe import book_fields, ledger_state
from kalshi_bot.btc_rules import verify_market
from kalshi_bot.btc_validate import edge, evaluate, scores
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.paper import PaperBroker
from tests.unit.test_btc import BOOK, MARKET_DATA, NOW, OPEN, SERIES_DATA

BASE = "https://demo-api.kalshi.co/trade-api/v2"


@pytest.fixture
def account(tmp_path: Path) -> Path:
    ledger = tmp_path / "paper.db"
    PaperBroker(ledger).close()
    return ledger


def headers() -> dict[str, str]:
    return {"Date": format_datetime(datetime.fromtimestamp(NOW, UTC), usegmt=True)}


def freeze(monkeypatch: pytest.MonkeyPatch, value: float = NOW) -> None:
    monkeypatch.setattr(
        btc_collect,
        "time",
        SimpleNamespace(time=lambda: value, monotonic=lambda: 100.0),
    )
    # Base observer must share only this isolated test clock, not global time.
    from kalshi_bot import btc_observe

    monkeypatch.setattr(
        btc_observe, "time", SimpleNamespace(time=lambda: value, monotonic=lambda: 100.0)
    )


def test_no_fabricated_timestamps_or_quotes() -> None:
    assert quote_timestamp(BOOK, NOW)["quote_timestamp"] is None
    assert quote_timestamp({"updated_time": NOW}, NOW)["quote_timestamp_status"] == "unavailable"
    assert quote_timestamp({"quote_ts": NOW - 1}, NOW)["quote_age_s"] == 1
    assert (
        quote_timestamp({"quote_ts": NOW + 1}, NOW)["quote_timestamp_status"] == "invalid_or_stale"
    )
    assert quote_timestamp({"quote_ts": "bad"}, NOW)["quote_timestamp_status"] == "invalid_or_stale"
    assert classify_book(book_fields(BOOK)) == "two_sided"
    partial = book_fields({"orderbook_fp": {"yes_dollars": [], "no_dollars": [[".6", "2"]]}})
    assert classify_book(partial) == "no_only"
    assert partial["yes_bid"] is None and partial["no_ask"] is None
    assert partial["yes_bid_depth"] == 0


def test_resume_is_idempotent_locked_and_preserves_deadline(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch)
    path = tmp_path / "capture.db"
    before = ledger_state(account)
    collector = Collector(path, account)
    deadline = collector.manifest["deadline"]
    collector.slot = 0
    collector.record_observation(NOW, None, False, True, False, {"errors": ["gap"]})
    collector.record_observation(NOW, None, False, True, False, {"errors": ["duplicate"]})
    collector.db.execute("INSERT INTO slots VALUES(0,?,NULL,0,'started')", (NOW,))
    collector.db.commit()
    with pytest.raises(OSError):
        Collector(path, account, resume=True)
    collector.close()  # Simulate process disappearing without finalization.
    resumed = Collector(path, account, resume=True)
    assert resumed.manifest["deadline"] == deadline
    assert resumed.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    assert resumed.db.execute("SELECT state FROM slots").fetchone()[0] == "interrupted"
    assert resumed.ticks == []
    assert ledger_state(account) == before
    resumed.close()
    with pytest.raises(ValueError, match="mismatch"):
        Collector(path, account, seconds=100, resume=True)


@pytest.mark.asyncio
async def test_detail_hydration_without_relaxing_strike_or_status(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch)
    collector = Collector(tmp_path / "capture.db", account)
    pending = copy.deepcopy(MARKET_DATA)
    pending.pop("floor_strike")
    with respx.mock(assert_all_called=True) as api:
        api.get(BASE + "/series/KXBTC15M").respond(
            200,
            json={"series": SERIES_DATA},
            headers=headers(),
        )
        listing = api.get(BASE + "/markets").respond(
            200,
            json={"markets": [pending], "cursor": ""},
            headers=headers(),
        )
        detail = api.get(BASE + "/markets/" + MARKET_DATA["ticker"]).respond(
            200,
            json={"market": MARKET_DATA},
            headers=headers(),
        )
        async with KalshiClient("https://demo-api.kalshi.co") as client:
            await collector.discover(client._client)
            assert len(collector.active) == 1
            assert collector.active[0].strike == MARKET_DATA["floor_strike"]
            params = [
                json.loads(r[0])
                for r in collector.db.execute("SELECT parameters FROM request_parameters")
            ]
            assert any(p.get("max_close_ts") == int(NOW) + 1800 for p in params)
            assert all(p.get("status") is None for p in params)
            # During backoff, no additional request.
            await collector.discover(client._client)
            assert listing.call_count == detail.call_count == 1
            detail.respond(200, json={"market": pending}, headers=headers())
            collector.next_discovery = 0
            await collector.discover(client._client)
            assert collector.active == []
            reason = json.loads(
                collector.db.execute(
                    "SELECT rejected FROM discovery ORDER BY at DESC,rowid DESC LIMIT 1"
                ).fetchone()[0]
            )[0]["reason"]
            assert "floor_strike" in reason
    collector.close()


@pytest.mark.asyncio
async def test_candidate_book_quarantined_with_reference_and_no_orders(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch)
    collector = Collector(tmp_path / "capture.db", account)
    collector.slot = 1
    before = ledger_state(account)
    pending = copy.deepcopy(MARKET_DATA)
    pending.pop("floor_strike")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("No broker may be constructed during observation.")

    monkeypatch.setattr(PaperBroker, "__init__", forbidden)
    with respx.mock(assert_all_called=True) as api:
        api.get(BASE + "/series/KXBTC15M").respond(
            200,
            json={"series": SERIES_DATA},
            headers=headers(),
        )
        api.get(BASE + "/markets").respond(
            200,
            json={"markets": [pending], "cursor": ""},
            headers=headers(),
        )
        api.get(BASE + "/markets/" + MARKET_DATA["ticker"]).respond(
            200,
            json={"market": pending},
            headers=headers(),
        )
        api.get(BASE + "/markets/" + MARKET_DATA["ticker"] + "/orderbook").respond(
            200,
            json=BOOK,
            headers=headers(),
        )
        api.get("https://api.exchange.coinbase.com/products/BTC-USD/ticker").respond(
            200,
            json={
                "time": datetime.fromtimestamp(NOW, UTC).isoformat(),
                "price": "73500",
                "bid": "73499",
                "ask": "73501",
            },
            headers=headers(),
        )
        async with KalshiClient("https://demo-api.kalshi.co") as kalshi, httpx.AsyncClient() as cb:
            await collector.cycle(kalshi._client, cb)
        assert all(call.request.method == "GET" for call in api.calls)
    stored = json.loads(collector.db.execute("SELECT payload FROM candidate_books").fetchone()[0])
    assert stored["verified"] is False and stored["strike_for_model"] is None
    assert stored["quote_timestamp"] is None
    assert collector.db.execute("SELECT count(*) FROM references_seen").fetchone()[0] == 1
    assert collector.db.execute("SELECT count(*) FROM markets").fetchone()[0] == 0
    assert ledger_state(account) == before
    collector.close()


def test_quality_policy_distinguishes_partial_books_from_failed_transport(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch)
    collector = Collector(tmp_path / "capture.db", account)
    collector.active = [verify_market(MARKET_DATA, SERIES_DATA, now=NOW)]
    collector.slot = 1
    payload = {
        **book_fields({"orderbook_fp": {"yes_dollars": [], "no_dollars": [[".6", "1"]]}}),
        "reference_valid": True,
        "errors": ["One-sided book; complete executable spread unavailable."],
    }
    collector.record_observation(NOW, MARKET_DATA["ticker"], False, True, False, payload)
    row = json.loads(collector.db.execute("SELECT payload FROM observations").fetchone()[0])
    assert row["data_healthy"] and row["book_state"] == "no_only"
    assert not row["transport_and_reference_valid"]
    collector.last_healthy = NOW - 61
    assert "60 seconds" in str(collector.quality_reason(NOW))
    collector.active = []
    assert collector.quality_reason(NOW) is None  # Rollover-specific allowance.
    collector.last_verified = NOW - 181
    assert "180 seconds" in str(collector.quality_reason(NOW))
    collector.last_verified = collector.last_healthy = NOW
    collector.db.executemany(
        "INSERT INTO slots VALUES(?,?,?,?,'finished')",
        [(i, NOW, NOW, 0 if i < 61 else 1) for i in range(150)],
    )
    assert "40%" in str(collector.quality_reason(NOW))
    collector.close()


@pytest.mark.asyncio
async def test_provider_specific_failure_breaker(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch)
    collector = Collector(tmp_path / "capture.db", account)
    with respx.mock(assert_all_called=True) as api:
        api.get("https://example.invalid/kalshi").respond(503, json={}, headers=headers())
        api.get("https://example.invalid/coinbase").respond(200, json={}, headers=headers())
        async with httpx.AsyncClient() as client:
            for _ in range(9):
                await collector.get(client, "kalshi-demo", "https://example.invalid/kalshi")
                await collector.get(client, "coinbase", "https://example.invalid/coinbase")
            with pytest.raises(btc_collect.QualityStop):
                await collector.get(client, "kalshi-demo", "https://example.invalid/kalshi")
    collector.close()


def test_terminal_stop_cannot_be_resumed_and_legacy_not_modified(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch)
    path = tmp_path / "capture.db"
    collector = Collector(path, account)
    collector.summary("quality_stop")
    collector.close()
    with pytest.raises(ValueError, match="Terminal"):
        Collector(path, account, resume=True)
    legacy = tmp_path / "legacy.db"
    with sqlite3.connect(legacy) as db:
        db.execute("CREATE TABLE run(key TEXT,value TEXT)")
    content = legacy.read_bytes()
    with pytest.raises(ValueError, match="Not a resumable"):
        Collector(legacy, account, resume=True)
    assert legacy.read_bytes() == content


@pytest.mark.asyncio
async def test_six_hour_loop_boundary_offline_no_broker(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This historical runner test remains purely synthetic; runtime gate stays sealed.
    monkeypatch.setattr(btc_collect, "require_collection_release", lambda: None)
    clock = [NOW]
    from kalshi_bot import btc_observe

    fake = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: clock[0] - NOW + 100)
    monkeypatch.setattr(btc_collect, "time", fake)
    monkeypatch.setattr(btc_observe, "time", fake)
    count = [0]

    async def cycle(self: Collector, kalshi: Any, coinbase: Any) -> None:
        count[0] += 1
        self.record_observation(clock[0], "offline-fixture", True, False, False, {})
        self.last_verified = self.last_healthy = clock[0]
        clock[0] += 2

    monkeypatch.setattr(Collector, "cycle", cycle)
    before = ledger_state(account)
    result = await btc_collect.collect(tmp_path / "six-hours.db", account)
    assert result["status"] == "completed"
    assert count[0] == 10800
    assert result["observations"] == 10800
    assert result["api_requests"] == 0
    assert ledger_state(account) == before


def test_validation_metrics_and_fee_adjusted_edges() -> None:
    metric = scores([{"probability": 0.8, "outcome": 1}, {"probability": 0.3, "outcome": 0}], 0.5)
    assert metric["brier"] == pytest.approx(0.065)
    assert metric["log_loss"] == pytest.approx(-(0.5 * math.log(0.8 * 0.7)))
    assert metric["independent_markets"] == 2
    assert edge(0.7, 0.5) == pytest.approx(0.17)
    assert edge(0.7, None) is None
    assert scores([])["brier"] is None


def test_recovery_after_unclean_process_exit(
    tmp_path: Path,
    account: Path,
) -> None:
    path = tmp_path / "crash.db"
    code = """
import os,sys,time
from pathlib import Path
from kalshi_bot.btc_collect import Collector
c=Collector(Path(sys.argv[1]),Path(sys.argv[2]))
c.slot=0
c.db.execute("INSERT INTO slots VALUES(0,?,NULL,0,'started')",(time.time(),))
c.record_observation(time.time(),None,False,True,False,{"errors":["offline crash test"]})
c.db.commit()
os._exit(17)
"""
    crashed = subprocess.run([sys.executable, "-c", code, str(path), str(account)], check=False)
    assert crashed.returncode == 17
    recovered = Collector(path, account, resume=True)
    assert recovered.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert recovered.db.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    assert recovered.db.execute("SELECT state FROM slots").fetchone()[0] == "interrupted"
    recovered.close()


def test_expired_deadline_and_empty_book_do_not_pass_quality(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch)
    path = tmp_path / "capture.db"
    collector = Collector(path, account, seconds=10)
    collector.slot = 0
    collector.record_observation(
        NOW,
        MARKET_DATA["ticker"],
        False,
        True,
        False,
        {
            **book_fields({"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}),
            "reference_valid": True,
            "errors": ["One-sided book; complete executable spread unavailable."],
        },
    )
    assert not json.loads(collector.db.execute("SELECT payload FROM observations").fetchone()[0])[
        "data_healthy"
    ]
    assert "wall clock" in str(collector.quality_reason(NOW - 2))
    collector.db.commit()
    collector.close()
    freeze(monkeypatch, NOW + 11)
    with pytest.raises(ValueError, match="deadline expired"):
        Collector(path, account, seconds=10, resume=True)


@pytest.mark.asyncio
async def test_finalized_labels_are_observed_without_settling_paper_account(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch, NOW + 301)
    collector = Collector(tmp_path / "capture.db", account)
    market = verify_market(MARKET_DATA, SERIES_DATA, now=NOW)
    collector.markets[market.ticker] = market
    collector.series = SERIES_DATA
    collector.db.execute(
        "INSERT INTO markets VALUES(?,?,?,NULL,NULL,NULL)",
        (market.ticker, NOW, json.dumps(asdict(market))),
    )
    collector.db.commit()
    final = {
        **MARKET_DATA,
        "status": "finalized",
        "result": "yes",
        "settlement_value_dollars": "1.0000",
        "settlement_ts": datetime.fromtimestamp(NOW + 301, UTC).isoformat(),
    }
    before = ledger_state(account)
    with respx.mock(assert_all_called=True) as api:
        api.get(BASE + "/markets/" + market.ticker).respond(
            200,
            json={"market": final},
            headers={
                "Date": format_datetime(datetime.fromtimestamp(NOW + 301, UTC), usegmt=True),
            },
        )
        async with KalshiClient("https://demo-api.kalshi.co") as client:
            await collector.outcomes(client._client)
    assert collector.db.execute("SELECT outcome FROM markets").fetchone()[0] == 1
    assert ledger_state(account) == before
    collector.close()


def test_chronological_validation_counts_markets_not_repeated_forecasts(
    tmp_path: Path,
    account: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    freeze(monkeypatch)
    path = tmp_path / "validation.db"
    collector = Collector(path, account)
    collector.db.execute(
        "INSERT INTO requests VALUES(NULL,?,?,?,?,?,?,?,?,?,?)",
        (
            OPEN - 1,
            OPEN - 1,
            "kalshi-demo",
            "/trade-api/v2/series/KXBTC15M",
            10,
            200,
            0,
            1,
            None,
            json.dumps({"series": SERIES_DATA}),
        ),
    )
    for i in range(6):
        opens, closes = OPEN + i * 900, OPEN + (i + 1) * 900
        raw = copy.deepcopy(MARKET_DATA)
        raw["ticker"] = f"KXBTC15M-TEST{i}-00"
        raw["event_ticker"] = f"KXBTC15M-TEST{i}"
        raw["open_time"] = datetime.fromtimestamp(opens, UTC).isoformat()
        raw["close_time"] = datetime.fromtimestamp(closes, UTC).isoformat()
        raw["expiration_time"] = datetime.fromtimestamp(closes + 86400, UTC).isoformat()

        def rule_time(stamp: float) -> str:
            return datetime.fromtimestamp(stamp, ZoneInfo("America/New_York")).strftime(
                "%I:%M %p %Z on %B %d, %Y"
            )

        raw["rules_primary"] = (
            "If the simple average of the sixty seconds of CF Benchmarks' BRTI before "
            + rule_time(closes)
            + " is at least the simple average of the sixty seconds "
            "of CF Benchmarks' BRTI before "
            + rule_time(opens)
            + ", then the market resolves to Yes."
        )
        market = verify_market(raw, SERIES_DATA, now=closes - 300)
        finalized = {
            **raw,
            "status": "finalized",
            "result": "yes",
            "settlement_value_dollars": "1.0000",
            "settlement_ts": datetime.fromtimestamp(closes + 5, UTC).isoformat(),
        }
        collector.db.execute(
            "INSERT INTO markets VALUES(?,?,?,?,?,?)",
            (
                market.ticker,
                opens,
                json.dumps(asdict(market)),
                1,
                closes + 30,
                json.dumps(finalized),
            ),
        )
        for offset in [0, 1, 2]:
            at = closes - 300 + offset
            payload = {
                "yes_probability": 0.7,
                "model_id": MODEL_ID,
                "reference_valid": True,
                "entries_enabled": False,
                "market_metadata": raw,
                "yes_ask": 0.5,
                "no_ask": 0.6,
                "book_received_at": at,
                "book_http_age_s": 0,
                "book_latency_ms": 10,
            }
            collector.db.execute(
                "INSERT INTO observations VALUES(NULL,?,?,?,?,?,?)",
                (at, market.ticker, 1, 0, 0, json.dumps(payload)),
            )
    collector.db.commit()
    before = path.read_bytes()
    result = evaluate(path, as_of=OPEN + 7 * 900)
    assert result["independent_markets"] == 6  # Not 18 repeated forecasts.
    assert result["development_metrics"]["independent_markets"] == 4
    assert result["test_metrics"]["independent_markets"] == 2
    assert result["baseline_prior_resolved_markets"] == 4
    assert result["test_metrics"]["brier"] == pytest.approx(0.09)
    assert result["entries_enabled"] is False and result["calibration_ready"] is False
    assert path.read_bytes() == before
    earlier = evaluate(path, as_of=OPEN + 2 * 900 + 29)
    assert earlier["independent_markets"] == 1  # Second label not yet received.
    fixed = evaluate(path, as_of=OPEN + 7 * 900, split_at=OPEN + 4 * 900)
    assert fixed["development_metrics"]["independent_markets"] == 4
    assert fixed["test_metrics"]["independent_markets"] == 2
    partial = evaluate(path, as_of=OPEN + 5 * 900 + 31, split_at=OPEN + 4 * 900)
    assert partial["development_metrics"]["independent_markets"] == 4
    assert partial["test_metrics"]["independent_markets"] == 1
    assert partial["test_metrics"]["baseline_probability_from_prior_labels"] == pytest.approx(5 / 6)
    assert partial["prospective_registration_verified"] is False
    with pytest.raises(ValueError, match="chronological"):
        evaluate(path, as_of=OPEN + 7 * 900, split_at=OPEN + 8 * 900)
    collector.close()
