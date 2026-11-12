"""Mock-HTTP demo integration only. No real sockets, campaign, account or trial."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from kalshi_bot import btc_collect, btc_demo_bridge, btc_observe
from kalshi_bot.btc_campaign_validate import evaluate_campaign
from kalshi_bot.btc_dashboard import render, snapshot
from kalshi_bot.btc_demo_bridge import DemoBridge, encode, proxy_guard
from kalshi_bot.btc_observe import ledger_state
from kalshi_bot.btc_supervisor import Supervisor, SupervisorStopped
from kalshi_bot.paper import PaperBroker
from kalshi_bot.safety import SimulationOnlyError
from tests.unit.test_btc import BOOK, NOW, SERIES_DATA, history
from tests.unit.test_btc_supervisor import MONO, START, frame, health, protocol, raw_market


@pytest.fixture
def context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    clock = [NOW]
    fake = SimpleNamespace(time=lambda: clock[0], monotonic=lambda: MONO + clock[0] - START)
    for module in (btc_collect, btc_observe, btc_demo_bridge):
        monkeypatch.setattr(module, "time", fake)
    ledger = tmp_path / "paper.db"
    PaperBroker(ledger).close()
    s = Supervisor(tmp_path / "campaign.db", ledger, protocol(ledger), now=START, monotonic=MONO)
    bridge = DemoBridge(s)
    yield s, bridge, ledger, clock
    s.close()


@pytest.fixture
def mock_demo(monkeypatch: pytest.MonkeyPatch):
    # Explicitly isolated collection release. The autouse no_network fixture
    # continues blocking sockets, and trade gates are NEVER patched here.
    monkeypatch.setattr(btc_demo_bridge, "require_collection_release", lambda: None)
    options: dict[str, Any] = {"calls": [], "markets": {}, "quote_timestamp": True}
    original_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        now = btc_demo_bridge.time.time()
        options["calls"].append((request.method, str(request.url)))
        assert request.method == "GET"
        current = raw_market(now)
        options["markets"].setdefault(current["ticker"], copy.deepcopy(current))
        headers = {"Date": format_datetime(datetime.fromtimestamp(now, UTC), usegmt=True)}
        if request.url.host == "api.exchange.coinbase.com":
            body = {
                "price": "73500",
                "bid": "73499",
                "ask": "73501",
                "time": datetime.fromtimestamp(now, UTC).isoformat(),
            }
        elif request.url.path.endswith("/series/KXBTC15M"):
            body = {"series": SERIES_DATA}
        elif request.url.path.endswith("/markets"):
            candidate = copy.deepcopy(current)
            if options.get("missing_strike"):
                candidate.pop("floor_strike")
            body = {"markets": [candidate], "cursor": ""}
        elif request.url.path.endswith("/orderbook"):
            body = copy.deepcopy(BOOK)
            if options["quote_timestamp"]:
                body["quote_ts"] = now - options.get("quote_age", 0)
            if options.get("one_sided"):
                body["orderbook_fp"]["yes_dollars"] = []
        else:
            ticker = request.url.path.split("/")[-1]
            candidate = copy.deepcopy(options["markets"][ticker])
            closes = datetime.fromisoformat(candidate["close_time"]).timestamp()
            if now >= closes:
                candidate.update(
                    status="finalized",
                    result="yes",
                    settlement_value_dollars="1.0000",
                    settlement_ts=candidate["close_time"],
                )
            if options.get("missing_strike"):
                candidate.pop("floor_strike")
            body = {"market": candidate}
        return httpx.Response(200, json=body, headers=headers)

    class MockClient(original_client):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", MockClient)
    return options


@pytest.mark.asyncio
async def test_every_collection_entrypoint_refuses_before_io(context, tmp_path: Path) -> None:
    s, bridge, ledger, _ = context
    before = ledger_state(ledger)
    with patch.object(httpx.AsyncClient, "__init__", side_effect=AssertionError("No client")):
        for operation in (
            bridge.poll_once(health(NOW)),
            bridge.run(lambda: health(NOW), asyncio.Event()),
            btc_collect.collect(tmp_path / "forbidden.db", ledger),
            btc_observe.run_capture(10, 2, tmp_path / "forbidden2.db", ledger),
        ):
            with pytest.raises(SimulationOnlyError, match="Collection disabled"):
                await operation
    assert not (tmp_path / "forbidden.db").exists()
    assert not (tmp_path / "forbidden2.db").exists()
    assert s.report()["recorded_slots"] == 0 and ledger_state(ledger) == before


@pytest.mark.asyncio
async def test_discovery_quotes_one_database_exact_retry_and_no_broker(
    context, mock_demo, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, bridge, ledger, _ = context
    before = ledger_state(ledger)
    bridge.collector.ticks = history(end=NOW - 1)
    monkeypatch.setattr(PaperBroker, "__init__", lambda *a, **k: pytest.fail("No broker"))
    result = await bridge.poll_once(health(NOW))
    calls = len(mock_demo["calls"])
    assert result["valid_observations"] == 1 and result["verified_distinct_markets"] == 1
    assert result["discovery"]["attempts"] == result["discovery"]["successes"] == 1
    assert not result["calibration_ready"] and not result["entries_enabled"]
    assert [Path(r[2]).resolve() for r in s.db.execute("PRAGMA database_list") if r[2]] == [
        s.path.resolve()
    ]
    assert s.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == calls
    assert s.db.execute("SELECT COUNT(*) FROM supervisor_candidates").fetchone()[0] == 1
    await bridge.poll_once(health(NOW))
    assert len(mock_demo["calls"]) == calls
    assert s.report()["recorded_slots"] == 1 and ledger_state(ledger) == before
    dashboard = snapshot(ledger, s.path, now=NOW)
    assert dashboard["campaign"]["required"]["test"] == 200
    assert dashboard["campaign"]["collection_release"] == "sealed"
    assert "Validation campaign" in render(dashboard)


@pytest.mark.asyncio
@pytest.mark.parametrize("option", ["missing_strike", "missing_timestamp", "stale_timestamp"])
async def test_unverified_or_stale_data_is_retained_but_never_eligible(
    context, mock_demo, option: str
) -> None:
    s, bridge, _, _ = context
    if option == "missing_timestamp":
        mock_demo["quote_timestamp"] = False
    elif option == "stale_timestamp":
        mock_demo["quote_age"] = 6
    else:
        mock_demo["missing_strike"] = True
    r = await bridge.poll_once(health(NOW))
    assert r["valid_observations"] == 0 and r["incomplete_observations"] == 1
    assert s.db.execute("SELECT COUNT(*) FROM campaign_forecasts").fetchone()[0] == 0
    assert not r["calibration_ready"] and not r["entries_enabled"]
    if option == "missing_strike":
        assert r["verified_distinct_markets"] == 0
        assert s.db.execute("SELECT COUNT(*) FROM candidate_books").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_one_sided_book_preserves_missing_prices_and_separate_coverage(context, mock_demo):
    s, bridge, _, _ = context
    mock_demo["one_sided"] = True
    result = await bridge.poll_once(health(NOW))
    payload = json.loads(s.db.execute("SELECT payload FROM observations").fetchone()[0])
    assert payload["yes_bid"] is None and payload["no_ask"] is None
    assert result["one_sided_observations"] == 1
    assert result["valid_observations"] == 0 and result["incomplete_observations"] == 1


@pytest.mark.asyncio
async def test_restart_resets_warmup_not_counters_deadline_or_receipts(context, mock_demo):
    s, bridge, ledger, clock = context
    await bridge.poll_once(health(NOW))
    p, path = s.protocol, s.path
    s.close()
    clock[0] = NOW + 2
    resumed = Supervisor(
        path, ledger, p, resume=True, now=clock[0], monotonic=MONO + clock[0] - START
    )
    try:
        other = DemoBridge(resumed)
        assert other.collector.ticks == []
        await other.poll_once(health(clock[0]))
        assert resumed.report()["recorded_slots"] == 2
        assert resumed.report()["original_deadline"] == p.deadline
        assert (
            resumed.db.execute("SELECT COUNT(*) FROM bridge_requests").fetchone()[0]
            == (resumed.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
        )
    finally:
        resumed.close()


@pytest.mark.asyncio
async def test_crash_raw_request_errors_stop_before_next_network_read(context, mock_demo):
    s, bridge, _, _ = context
    # Source committed before a simulated crash, but campaign counters not yet acknowledged.
    for _ in range(10):
        s.db.execute(
            "INSERT INTO requests VALUES(NULL,?,?,'kalshi-demo','/trade-api/v2/markets',"
            "1,503,0,0,'HTTPStatusError','{}')",
            (NOW, NOW),
        )
    with pytest.raises(SupervisorStopped):
        await bridge.poll_once(health(NOW))
    assert mock_demo["calls"] == []
    assert s.report()["api_errors"] == 10
    assert s.control()["reason"] == "consecutive_provider_errors"
    assert s.db.execute("SELECT COUNT(*) FROM bridge_requests").fetchone()[0] == 10


@pytest.mark.asyncio
async def test_prepared_receipt_crash_retry_is_exact_or_expired_never_backfilled(
    context, mock_demo
):
    s, bridge, _, clock = context
    original = frame(NOW)
    encoded = encode(original)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    s.db.execute(
        "INSERT INTO bridge_batches VALUES(1,?,'prepared',?,NULL,?)", (NOW, encoded, digest)
    )
    bridge.health = health(NOW)
    bridge.recover()
    assert s.db.execute("SELECT sha256 FROM supervisor_frames").fetchone()[0] == digest
    s.db.execute(
        "UPDATE bridge_batches SET state='prepared',frame=? WHERE id=1", (encoded,)
    )  # synthetic crash after main receipt, before source ack
    bridge.recover()
    assert s.report()["recorded_slots"] == 1
    late = encode(frame(NOW + 2))
    s.db.execute(
        "INSERT INTO bridge_batches VALUES(2,?,'prepared',?,NULL,?)",
        (NOW + 2, late, hashlib.sha256(late.encode()).hexdigest()),
    )
    clock[0] = NOW + 6
    bridge.recover()
    assert s.report()["recorded_slots"] == 1
    assert s.db.execute("SELECT state FROM bridge_batches WHERE id=2").fetchone()[0] == (
        "expired_unrecorded"
    )


@pytest.mark.asyncio
async def test_rollover_and_finalized_outcome_through_real_collector_algorithms(context, mock_demo):
    s, bridge, ledger, clock = context
    before = ledger_state(ledger)
    bridge.collector.ticks = history(end=NOW - 1)
    for at in range(int(NOW), int(NOW + 302), 2):
        clock[0] = at
        await bridge.poll_once(health(at))
    r = s.report()
    assert r["verified_distinct_markets"] == 2
    assert r["finalized_verified_markets"] == 1
    assert (
        s.db.execute("SELECT COUNT(*) FROM campaign_forecasts WHERE outcome=1").fetchone()[0] == 1
    )
    assert r["status"] == "running" and not r["calibration_ready"]
    assert ledger_state(ledger) == before
    assert all(method == "GET" for method, _ in mock_demo["calls"])
    scored = evaluate_campaign(s.path, as_of=clock[0])
    assert scored["metrics"]["development"]["scores"]["independent_markets"] == 1
    assert scored["metrics"]["test"]["scores"] is None
    assert scored["metrics"]["selection"]["hypothetical_edges"] is None
    assert not scored["minimum_sample_counts_met"] and not scored["paper_trial_completed"]
    assert scored["paper_win_rate"] is None and scored["simulated_profitability"] is None
    assert (
        scored["metrics"]["development"]["hypothetical_edges"]["sides"]["yes"][
            "qualified_quote_count"
        ]
        == 1
    )
    assert (
        s.db.execute(
            "SELECT COUNT(*) FROM bridge_batches WHERE state='accepted' AND frame IS NOT NULL"
        ).fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_stopped_campaign_and_emergency_stop_event_do_not_poll(context, mock_demo):
    _, bridge, _, _ = context
    stop = asyncio.Event()
    stop.set()
    result = await bridge.run(lambda: health(NOW), stop)
    assert result["status"] == "operator_stop" and mock_demo["calls"] == []
    with pytest.raises(SupervisorStopped):
        await bridge.poll_once(health(NOW))
    assert mock_demo["calls"] == []


@pytest.mark.asyncio
async def test_proxy_guard_blocks_account_headers_and_other_hosts() -> None:
    for url, headers in (
        ("https://api.exchange.coinbase.com/products/ETH-USD/ticker", {}),
        ("https://api.elections.kalshi.com/trade-api/v2/markets", {}),
        ("https://api.exchange.coinbase.com/products/BTC-USD/ticker", {"Authorization": "test"}),
    ):
        with pytest.raises(SimulationOnlyError):
            await proxy_guard(httpx.Request("GET", url, headers=headers))


@pytest.mark.asyncio
async def test_deadline_stops_run_before_client_or_extra_data(context, mock_demo):
    s, bridge, _, clock = context
    clock[0] = s.protocol.deadline
    result = await bridge.run(lambda: health(clock[0]), asyncio.Event())
    assert result["status"] == "completed" and result["recorded_slots"] == 0
    assert mock_demo["calls"] == []


@pytest.mark.asyncio
async def test_timeout_cancellation_records_raw_error_without_inventing_quotes(
    context, mock_demo, monkeypatch: pytest.MonkeyPatch
):
    s, bridge, _, _ = context

    async def delayed(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        raise AssertionError("Must be cancelled before response")

    client = httpx.AsyncClient

    class DelayedClient(client):
        def __init__(self, *args, **kwargs):
            # Inherit the mock setup, replace only its fake transport.
            super().__init__(*args, **kwargs)
            self._transport = httpx.MockTransport(delayed)

    monkeypatch.setattr(httpx, "AsyncClient", DelayedClient)
    r = await bridge.poll_once(health(NOW))
    assert r["valid_observations"] == 0 and r["api_errors"] == 1
    assert s.db.execute("SELECT error FROM requests").fetchone()[0] == "CancelledError"
    assert s.db.execute("SELECT COUNT(*) FROM supervisor_candidates").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_unsafe_host_check_and_batch_corruption_fail_before_reads(context, mock_demo):
    s, bridge, _, _ = context
    frozen = encode(frame(NOW))
    s.db.execute(
        "INSERT INTO bridge_batches VALUES(1,?,'prepared',?,NULL,?)",
        (NOW, frozen, "a" * 64),
    )
    with pytest.raises(SupervisorStopped, match="fingerprint"):
        await bridge.poll_once(health(NOW))
    assert s.control()["reason"] == "corrupted_bridge_batch" and mock_demo["calls"] == []


def test_expired_raw_settlement_requires_fresh_refetch_not_freshened_timestamp(context):
    s, bridge, _, clock = context
    raw = raw_market(NOW)
    final = {
        **raw,
        "status": "finalized",
        "result": "yes",
        "settlement_ts": raw["close_time"],
        "settlement_value_dollars": "1.0000",
    }
    s.db.execute(
        "INSERT INTO markets VALUES(?,?,?,1,?,?)",
        (raw["ticker"], NOW, "{}", NOW + 300, json.dumps(final)),
    )
    clock[0] = NOW + 310
    bridge.recover()
    assert s.db.execute("SELECT outcome FROM markets").fetchone()[0] is None
    assert s.db.execute("SELECT COUNT(*) FROM supervisor_outcomes").fetchone()[0] == 0
    assert (
        s.db.execute(
            "SELECT COUNT(*) FROM supervisor_events WHERE kind='settlement_refresh_required'"
        ).fetchone()[0]
        == 1
    )
