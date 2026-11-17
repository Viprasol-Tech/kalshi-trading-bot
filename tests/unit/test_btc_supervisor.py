"""Synthetic supervisor drills only. No network, hosting, account entries or orders."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from kalshi_bot.btc_campaign import Protocol
from kalshi_bot.btc_campaign_validate import evaluate_campaign
from kalshi_bot.btc_observe import book_fields, ledger_state
from kalshi_bot.btc_supervisor import (
    Frame,
    HostHealth,
    Supervisor,
    SupervisorStopped,
    implementation_sha256,
    main,
    run_collection,
)
from kalshi_bot.hosting import backup, restore
from kalshi_bot.paper import PaperBroker
from kalshi_bot.safety import SimulationOnlyError, require_entry_release
from tests.unit.test_btc import MARKET_DATA, NOW, SERIES_DATA, history
from tests.unit.test_btc_readiness import executable_payload

START = NOW - 30
MONO = 100.0


def raw_market(at: float) -> dict:
    opens = at // 900 * 900
    closes = opens + 900

    def utc(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, UTC).isoformat()

    def rule(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, ZoneInfo("America/New_York")).strftime(
            "%I:%M %p %Z on %b %d, %Y"
        )

    event = "KXBTC15M-" + str(int(closes))
    return {
        **MARKET_DATA,
        "ticker": event + "-15",
        "event_ticker": event,
        "open_time": utc(opens),
        "close_time": utc(closes),
        "expiration_time": utc(closes + 86400),
        "rules_primary": (
            "If the simple average of the sixty seconds of CF Benchmarks' BRTI before "
            + rule(closes)
            + " is at least the simple average of the sixty seconds "
            "of CF Benchmarks' BRTI before " + rule(opens) + ", then the market resolves to Yes."
        ),
    }


def frame(at: float, **changes) -> Frame:
    payload = executable_payload()
    payload.update(
        market_metadata=raw_market(at),
        quote_timestamp=at - 1,
        quote_timestamp_raw=at - 1,
        yes_probability=0.001,  # supervisor must ignore this caller forecast
    )
    for name in ("book", "reference", "metadata"):
        payload[name + "_received_at"] = at
    payload.update(changes)
    return Frame(
        at,
        payload,
        copy.deepcopy(SERIES_DATA),
        tuple(history(end=at)),
        (("kalshi-demo", True), ("coinbase", True)),
        (True,),
    )


def health(at: float) -> HostHealth:
    return HostHealth(at, at - 30, 20 * 1024**3, 25 * 1024**3, 0.1)


def protocol(ledger: Path, **changes) -> Protocol:
    import kalshi_bot.btc_model as model

    return replace(
        Protocol(
            START - 10,
            START,
            START + 90030,
            START + 180030,
            START + 604800,
            implementation_sha256(),
            ledger_state(ledger)["logical_sha256"],
            hashlib.sha256(Path(model.__file__).read_bytes()).hexdigest(),
        ),
        **changes,
    )


@pytest.fixture
def setup(tmp_path: Path):
    ledger = tmp_path / "paper.db"
    PaperBroker(ledger).close()
    p = protocol(ledger)
    supervisor = Supervisor(tmp_path / "campaign.db", ledger, p, now=START, monotonic=MONO)
    yield supervisor, ledger, p
    supervisor.close()


def ingest(s: Supervisor, at: float, f: Frame | None = None) -> bool:
    return s.ingest(f or frame(at), now=at, monotonic=MONO + at - START, health=health(at))


def advance(s: Supervisor, at: float) -> dict:
    return s.advance(now=at, monotonic=MONO + at - START, health=health(at))


def test_current_slot_dedup_model_recomputation_and_no_account_mutation(setup) -> None:
    s, ledger, _ = setup
    before = ledger_state(ledger)
    assert ingest(s, START)
    assert not ingest(s, START)
    assert ingest(s, NOW)
    for at in range(int(NOW + 2), int(NOW + 36), 2):
        ingest(s, at)
    r = s.report()
    assert r["recorded_slots"] == 19 and r["valid_observations"] == 19
    assert r["verified_distinct_markets"] == 1
    assert r["discovery"]["attempts"] == 19
    row = s.db.execute("SELECT * FROM campaign_forecasts").fetchone()
    assert row["predicted_at"] == NOW
    assert row["probability"] != 0.001
    assert not r["collection_started"] and not r["entries_enabled"] and not r["live_enabled"]
    assert ledger_state(ledger) == before


def test_conflicting_retry_persists_terminal_stop(setup) -> None:
    s, _, _ = setup
    ingest(s, START)
    with pytest.raises(SupervisorStopped):
        ingest(s, START, frame(START, extra="different evidence"))
    assert s.control()["status"] == "safety_stop"
    assert s.report()["recorded_slots"] == 1


def test_lock_refuses_concurrent_process_and_resume_preserves_deadline(setup) -> None:
    s, ledger, p = setup
    ingest(s, START)
    with pytest.raises((OSError, BlockingIOError)):
        Supervisor(s.path, ledger, p, resume=True, now=START, monotonic=MONO)
    path = s.path
    s.close()
    other = Supervisor(path, ledger, p, resume=True, now=START + 4, monotonic=500)
    try:
        assert other.ingest(
            frame(START + 4), now=START + 4, monotonic=500, health=health(START + 4)
        )
        assert other.report()["original_deadline"] == p.deadline
        assert other.report()["unrecorded_slots"] == 1
        assert other.report()["resumes"] == 1
        assert other.db.execute("SELECT COUNT(*) FROM supervisor_frames").fetchone()[0] == 2
    finally:
        other.close()


def test_outage_backfill_refused(setup) -> None:
    s, _, _ = setup
    ingest(s, START)
    with pytest.raises(ValueError, match="backfill"):
        s.ingest(frame(START + 2), now=START + 8, monotonic=MONO + 8, health=health(START + 8))
    assert s.report()["recorded_slots"] == 1


@pytest.mark.parametrize("kind", ["quality_stop", "operator_stop", "completed"])
def test_terminal_state_cannot_resume_even_if_health_recovers(setup, kind: str) -> None:
    s, ledger, p = setup
    if kind == "quality_stop":
        r = advance(s, START + 61)
        assert r["stop_reason"] == "no_healthy_sample"
    elif kind == "operator_stop":
        s.stop(now=START)
    else:
        advance(s, p.deadline)
    assert s.control()["status"] == kind
    path = s.path
    s.close()
    with pytest.raises(SupervisorStopped, match="Terminal"):
        Supervisor(path, ledger, p, resume=True, now=p.deadline + 1, monotonic=1000)


def test_clock_rollback_and_forward_jump_are_sticky(setup) -> None:
    s, _, _ = setup
    ingest(s, START)
    result = s.advance(now=START + 10, monotonic=MONO + 1, health=health(START + 10))
    assert result["status"] == "safety_stop"
    assert result["stop_reason"] == "clock_discontinuity"
    assert advance(s, START + 12)["status"] == "safety_stop"


def test_resume_clock_rollback_is_persisted_without_reset(setup) -> None:
    s, ledger, p = setup
    ingest(s, START + 2)
    path = s.path
    s.close()
    other = Supervisor(path, ledger, p, resume=True, now=START + 1, monotonic=500)
    try:
        assert other.control()["status"] == "safety_stop"
        assert other.control()["reason"] == "clock_regressed_on_resume"
    finally:
        other.close()


@pytest.mark.parametrize(
    "change",
    [
        {"backup_confirmed_at": START - 901},
        {"sampled_at": START - 6},
        {"free_bytes": 100},
        {"clock_offset_seconds": 1.1},
        {"backup_confirmed_at": START + 1},
        {"total_bytes": 0},
    ],
)
def test_infrastructure_stop_is_persistent(setup, change: dict) -> None:
    s, _, _ = setup
    result = s.advance(now=START, monotonic=MONO, health=replace(health(START), **change))
    assert result["status"] == "quality_stop"
    assert result["stop_reason"] == "host_backup_clock_or_storage_unhealthy"


def test_missing_unverified_strike_and_event_timestamp_never_make_forecast(setup) -> None:
    s, _, _ = setup
    missing = frame(START)
    missing.payload["market_metadata"]["floor_strike"] = None
    assert ingest(s, START, missing)
    assert ingest(s, START + 2, frame(START + 2, quote_timestamp_status="unavailable"))
    r = s.report()
    assert r["valid_observations"] == 0 and r["incomplete_observations"] == 2
    assert s.db.execute("SELECT COUNT(*) FROM supervisor_candidates").fetchone()[0] == 0
    assert advance(s, START + 61)["status"] == "quality_stop"


def test_consecutive_provider_failures_do_not_reset_on_segment_or_resume(setup) -> None:
    s, _, _ = setup
    bad = replace(frame(START), requests=(("kalshi-demo", False),) * 10)
    with pytest.raises(SupervisorStopped):
        ingest(s, START, bad)
    assert s.control()["reason"] == "consecutive_provider_errors"
    assert s.report()["discovery"]["api_errors"] == 10


def test_rolling_window_counts_missing_slots_not_only_received_frames(setup) -> None:
    s, _, _ = setup
    # Regular valid samples prevent the 60s timeout but missing slots still fail
    # the 150-slot >=300s coverage rule. No missing row is manufactured.
    for at in range(int(START), int(START + 298), 30):
        ingest(s, at)
    with pytest.raises(SupervisorStopped):
        ingest(s, START + 298)
    assert s.control()["reason"] == "unusable_or_missing_window_fraction"


def test_six_hour_boundaries_remain_fixed_after_restart(setup) -> None:
    s, ledger, p = setup
    # An explicit isolated state fixture represents earlier synthetic healthy
    # history; it is not provider data and never becomes validation evidence.
    boundary = START + 21600
    with s.transaction():
        s.db.execute(
            "UPDATE supervisor_control SET last_wall=?,last_healthy=?,last_verified=?",
            (boundary - 2,) * 3,
        )
        s.db.execute("INSERT INTO supervisor_segments VALUES(0,?,?,'active')", (START, boundary))
    path = s.path
    s.close()
    other = Supervisor(path, ledger, p, resume=True, now=boundary + 2, monotonic=500)
    try:
        r = other.advance(now=boundary + 2, monotonic=500, health=health(boundary + 2))
        assert r["status"] == "running"
        windows = other.db.execute("SELECT * FROM supervisor_segments ORDER BY segment").fetchall()
        assert windows[0]["state"] == "window_elapsed"
        assert windows[1]["starts"] == boundary
        assert windows[1]["deadline"] == START + 43200
    finally:
        other.close()


def test_registry_and_receipt_commit_rollback_is_recoverable(setup) -> None:
    s, _, _ = setup
    with (
        patch.object(
            s, "_flush_forecasts", side_effect=[None, RuntimeError("synthetic power loss")]
        ),
        pytest.raises(RuntimeError, match="power loss"),
    ):
        ingest(s, START)
    assert s.report()["recorded_slots"] == 0
    assert s.db.execute("SELECT COUNT(*) FROM supervisor_candidates").fetchone()[0] == 0
    assert ingest(s, START)
    assert s.report()["recorded_slots"] == 1


def test_400_synthetic_labels_do_not_unlock_collection_or_entries(setup) -> None:
    s, _, p = setup
    # Registry fixtures test quotas/sealing, NOT actual sample acquisition or
    # authenticity. Keep the supervisor's verified/observed counts at zero.
    for i in range(400):
        at = (
            p.starts_at + i * 500
            if i < 100
            else p.development_ends_at + (i - 100) * 500
            if i < 200
            else p.test_starts_at + (i - 200) * 500
        )
        ticker = f"KXBTC15M-SYNTHETIC-{i}"
        s.registry.forecast(ticker, at, at + 300, 0.5, "d" * 64)
        s.registry.outcome(ticker, i % 2, at + 301, "e" * 64)
    report = s.report()
    assert report["minimum_sample_counts_met"]
    assert report["score_eligible_distinct_markets"] == {
        "development": 100,
        "selection": 100,
        "test": 200,
    }
    assert report["verified_distinct_markets"] == 0 and report["recorded_slots"] == 0
    assert not report["data_provenance_verified"] and not report["entries_enabled"]
    scored = evaluate_campaign(s.path, as_of=p.test_starts_at + 100000)
    assert scored["metrics"]["test"]["sealed"] and scored["metrics"]["test"]["scores"] is None
    with pytest.raises(SimulationOnlyError):
        run_collection()
    with pytest.raises(SimulationOnlyError):
        require_entry_release()


def test_frozen_protocol_deadline_and_model_ledger_changes_refused(setup) -> None:
    s, ledger, p = setup
    path = s.path
    s.close()
    with pytest.raises(ValueError, match="protocol changed"):
        Supervisor(
            path,
            ledger,
            replace(p, deadline=p.deadline - 1),
            resume=True,
            now=START,
            monotonic=MONO,
        )
    with closing(sqlite3.connect(ledger)) as db:
        db.execute("UPDATE account SET cash=4999")
        db.commit()
    with pytest.raises(ValueError, match="ledger mismatch"):
        Supervisor(path, ledger, p, resume=True, now=START, monotonic=MONO)


def test_settlement_must_be_finalized_and_match_verified_strike(setup) -> None:
    s, _, _ = setup
    for at in range(int(START), int(NOW + 302), 2):
        ingest(s, at)
    original = raw_market(NOW)
    closed = datetime.fromisoformat(original["close_time"]).timestamp()
    final = {
        **original,
        "status": "finalized",
        "result": "yes",
        "settlement_ts": datetime.fromtimestamp(closed, UTC).isoformat(),
        "settlement_value_dollars": "1.0000",
    }
    assert s.settlement(
        final,
        SERIES_DATA,
        received_at=closed,
        now=closed,
        monotonic=MONO + closed - START,
        health=health(closed),
    )
    assert not s.settlement(
        final,
        SERIES_DATA,
        received_at=closed + 1,
        now=closed + 1,
        monotonic=MONO + closed + 1 - START,
        health=health(closed + 1),
    )
    row = s.db.execute("SELECT * FROM campaign_forecasts").fetchone()
    assert row["outcome"] == 1 and row["confirmed_at"] == closed
    with pytest.raises(ValueError, match="finalized evidence"):
        s.settlement(
            {**final, "floor_strike": 1},
            SERIES_DATA,
            received_at=closed + 1,
            now=closed + 1,
            monotonic=MONO + closed + 1 - START,
            health=health(closed + 1),
        )


def test_expired_deadline_never_accepts_new_observation_or_outcome(setup) -> None:
    s, _, p = setup
    result = advance(s, p.deadline)
    assert result["status"] == "completed"
    assert result["expected_slots_through_checkpoint"] == 302400
    with pytest.raises(SupervisorStopped):
        ingest(s, p.deadline)
    assert s.report()["recorded_slots"] == 0


def test_one_sided_books_are_useful_but_reported_incomplete(setup) -> None:
    s, _, _ = setup
    f = frame(START)
    f.payload.update(
        book_fields({"orderbook_fp": {"yes_dollars": [], "no_dollars": [["0.55", "8"]]}})
    )
    assert ingest(s, START, f)
    r = s.report()
    assert r["usable_observations_including_one_sided"] == 1
    assert r["valid_observations"] == 0 and r["incomplete_observations"] == 1
    assert r["one_sided_observations"] == 1
    assert r["missing_or_incomplete_percentage"] == 100


def test_freshness_is_rechecked_at_receipt_not_only_declared_observation_time(setup) -> None:
    s, _, _ = setup
    f = frame(START)
    f.payload.update(quote_timestamp=START - 5, quote_timestamp_raw=START - 5)
    assert s.ingest(f, now=START + 1, monotonic=MONO + 1, health=health(START + 1))
    r = s.report()
    assert r["valid_observations"] == 0 and r["stale_observations"] == 1


def test_monotonic_rollback_with_small_wall_clock_change_still_stops(setup) -> None:
    s, _, _ = setup
    ingest(s, START + 2)
    r = s.advance(now=START + 2, monotonic=MONO + 1.5, health=health(START + 2))
    assert r["status"] == "safety_stop" and r["stop_reason"] == "clock_discontinuity"


def test_quality_window_history_is_not_reset_at_rollover(setup) -> None:
    s, _, _ = setup
    boundary = START + 21600
    slot = 10801
    # Synthetic fixture history only; no real observations or probability labels.
    with s.transaction():
        s.db.execute(
            "UPDATE supervisor_control SET last_wall=?,last_healthy=?,last_verified=?",
            (boundary - 2,) * 3,
        )
        for previous in range(slot - 149, slot):
            at = START + previous * 2
            s.db.execute(
                "INSERT INTO supervisor_frames VALUES(?,?,?,NULL,0,0,0,'unavailable',"
                "'synthetic missing','{}',?)",
                (previous, at, at, "a" * 64),
            )
        s.db.execute("UPDATE supervisor_totals SET recorded=149")
    with pytest.raises(SupervisorStopped):
        ingest(s, boundary + 2)
    assert s.control()["reason"] == "unusable_or_missing_window_fraction"


def test_clean_backup_restore_resumes_without_duplicate_or_deadline_extension(
    setup, tmp_path: Path
):
    s, ledger, p = setup
    ingest(s, START)
    before = ledger_state(ledger)
    saved = tmp_path / "backup"
    backup(tmp_path, saved)
    clean = tmp_path / "clean"
    restore(saved, clean)
    other = Supervisor(
        clean / "campaign.db", clean / "paper.db", p, resume=True, now=START + 4, monotonic=500
    )
    try:
        assert other.ingest(
            frame(START + 4), now=START + 4, monotonic=500, health=health(START + 4)
        )
        assert other.report()["original_deadline"] == p.deadline
        assert other.report()["recorded_slots"] == 2
        assert ledger_state(clean / "paper.db") == before
    finally:
        other.close()
    assert ledger_state(ledger) == before


def test_neutral_settlement_metadata_change_does_not_relabel_or_conflict(setup) -> None:
    s, _, _ = setup
    for at in range(int(START), int(NOW + 302), 2):
        ingest(s, at)
    original = raw_market(NOW)
    closed = datetime.fromisoformat(original["close_time"]).timestamp()
    final = {
        **original,
        "status": "finalized",
        "result": "yes",
        "settlement_ts": datetime.fromtimestamp(closed, UTC).isoformat(),
        "settlement_value_dollars": "1.0000",
    }
    kwargs = {
        "received_at": closed,
        "now": closed,
        "monotonic": MONO + closed - START,
        "health": health(closed),
    }
    assert s.settlement(final, SERIES_DATA, **kwargs)
    assert not s.settlement({**final, "volume": 100}, SERIES_DATA, **kwargs)
    assert s.control()["status"] == "running"
    assert s.report()["finalized_verified_markets"] == 1
    with pytest.raises(SupervisorStopped):
        s.settlement(
            {**final, "result": "no", "settlement_value_dollars": "0.0000"}, SERIES_DATA, **kwargs
        )
    assert s.control()["reason"] == "conflicting_finalized_outcome"


@pytest.mark.parametrize("value", ["true", "approved", "1"])
def test_environment_approvals_cannot_enable_collection(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("KALSHI_COLLECTION_APPROVED", value)
    monkeypatch.setenv("KALSHI_HOSTING_APPROVED", value)
    with pytest.raises(SimulationOnlyError, match="Offline supervisor"):
        run_collection()


def test_inspection_cli_readonly_and_missing_database_is_never_created(
    setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    s, ledger, _ = setup
    before = s.control()
    account_before = ledger_state(ledger)
    monkeypatch.setattr("sys.argv", ["btc_supervisor", str(s.path)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["offline_only"] and not result["collection_started"]
    assert s.control() == before and ledger_state(ledger) == account_before
    missing = tmp_path / "missing.db"
    monkeypatch.setattr("sys.argv", ["btc_supervisor", str(missing)])
    with pytest.raises(sqlite3.Error):
        main()
    assert not missing.exists()


def test_forecast_tie_chooses_earlier_observation_without_reading_labels(setup) -> None:
    s, _, _ = setup
    ingest(s, START)
    ingest(s, NOW - 2)
    ingest(s, NOW + 2)
    for at in range(int(NOW + 4), int(NOW + 36), 2):
        # The actual 300-second instant is deliberately missing in this fixture.
        ingest(s, at)
    assert s.db.execute("SELECT predicted_at FROM campaign_forecasts").fetchone()[0] == NOW - 2


def test_verified_strike_cannot_change_mid_market(setup) -> None:
    s, _, _ = setup
    ingest(s, START)
    changed = frame(START + 2)
    changed.payload["market_metadata"]["floor_strike"] += 1
    with pytest.raises(SupervisorStopped):
        ingest(s, START + 2, changed)
    assert s.control()["reason"] == "verified_market_identity_changed"
    assert s.report()["verified_distinct_markets"] == 1
