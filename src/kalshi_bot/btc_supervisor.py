"""Offline-only 400-market campaign coordinator. No network driver or orders.

Exercise this state machine with explicit recorded/synthetic inputs. It cannot
start a collector, connect a feed, provision hosting or authorize execution.
Declared input timestamps/host facts are not independently authenticated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from kalshi_bot.btc_campaign import CampaignRegistry, Protocol
from kalshi_bot.btc_collect import QUALITY_POLICY, _lock, classify_book
from kalshi_bot.btc_dashboard import connect_readonly
from kalshi_bot.btc_model import ReferenceTick, estimate
from kalshi_bot.btc_observe import book_fields, ledger_state
from kalshi_bot.btc_rules import utc_timestamp, verify_market
from kalshi_bot.btc_validate import executable_edges
from kalshi_bot.safety import SimulationOnlyError, require_simulation

POLICY = {
    "version": 1,
    "interval_seconds": 2,
    "segment_seconds": 21600,
    "maximum_clock_drift_seconds": 2,
    "maximum_backup_age_seconds": 900,
    "minimum_free_bytes": 2 * 1024**3,
    "minimum_free_fraction": 0.15,
    **QUALITY_POLICY,
}
# The host threshold is intentionally stricter than the old collector's 250MB.
POLICY["minimum_free_bytes"] = 2 * 1024**3
TERMINAL = frozenset({"completed", "quality_stop", "safety_stop", "operator_stop"})


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def implementation_sha256() -> str:
    root = Path(__file__).parent
    files = (
        "btc_supervisor.py",
        "btc_demo_bridge.py",
        "btc_campaign.py",
        "btc_campaign_validate.py",
        "btc_automation.py",
        "btc_strategy.py",
        "btc_reconcile.py",
        "paper.py",
        "btc_data.py",
        "btc_collect.py",
        "btc_observe.py",
        "btc_model.py",
        "btc_rules.py",
        "btc_validate.py",
        "safety.py",
        "exchange/client.py",
    )
    return hashlib.sha256(b"".join((root / name).read_bytes() for name in files)).hexdigest()


@dataclass(frozen=True)
class HostHealth:
    sampled_at: float
    backup_confirmed_at: float
    free_bytes: int
    total_bytes: int
    clock_offset_seconds: float

    def healthy(self, now: float) -> bool:
        return (
            all(
                finite(v)
                for v in (self.sampled_at, self.backup_confirmed_at, self.clock_offset_seconds)
            )
            and 0 <= now - self.sampled_at <= 5
            and 0 <= now - self.backup_confirmed_at <= 900
            and abs(self.clock_offset_seconds) <= 1
            and type(self.free_bytes) is int
            and type(self.total_bytes) is int
            and self.total_bytes > 0
            and 2 * 1024**3 <= self.free_bytes <= self.total_bytes
            and self.free_bytes / self.total_bytes >= 0.15
        )


@dataclass(frozen=True)
class Frame:
    at: float
    payload: dict[str, Any]
    series: dict[str, Any]
    ticks: tuple[ReferenceTick, ...] = ()
    # Ordered provider results, not a caller's aggregate "healthy" flag.
    requests: tuple[tuple[str, bool], ...] = ()
    discoveries: tuple[bool, ...] = ()


class SupervisorStopped(RuntimeError):
    """Terminal or not-yet-started coordinator; input was not applied."""


class Supervisor:
    """Single-host kernel lock plus atomic registry/receipts/checkpoints in ONE DB.

    Six-hour windows are anchored to the original campaign start, never to a
    process restart. Quality history and terminal stops span every window.
    Offline evidence is stored as declared, not certified as real market data.
    """

    def __init__(
        self,
        path: Path,
        ledger: Path,
        protocol: Protocol,
        *,
        resume: bool = False,
        now: float | None = None,
        monotonic: float | None = None,
    ) -> None:
        require_simulation()
        protocol.validate()
        now = time.time() if now is None else now
        monotonic = time.monotonic() if monotonic is None else monotonic
        if (
            not finite(now)
            or not finite(monotonic)
            or monotonic < 0
            or now < protocol.registered_at
            or path.resolve() == ledger.resolve()
            or path.is_symlink()
            or ledger.is_symlink()
        ):
            raise ValueError("Invalid coordinator identity/clock.")
        if path.exists() != resume:
            raise ValueError("New requires a new path; resume requires existing state.")
        self.protocol, self.path, self.ledger = protocol, path, ledger
        self.anchor_wall, self.anchor_mono = now, monotonic
        self.last_mono = monotonic
        self.mono_deadline = monotonic + max(0, protocol.deadline - now)
        self._verify_fingerprints()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = _lock(Path(str(path) + ".supervisor.lock"))
        try:
            self.registry = CampaignRegistry(path, protocol)
            self.db = self.registry.db
            if [row[0] for row in self.db.execute("PRAGMA quick_check")] != ["ok"]:
                raise ValueError("Coordinator SQLite integrity failed.")
            if (
                resume
                and not self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supervisor_manifest'"
                ).fetchone()
            ):
                raise ValueError("Not an existing coordinator; never adopt unknown state.")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS supervisor_manifest(
                    id INTEGER PRIMARY KEY CHECK(id=1), canonical TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_control(
                    id INTEGER PRIMARY KEY CHECK(id=1), status TEXT NOT NULL, reason TEXT,
                    last_wall REAL NOT NULL, last_verified REAL NOT NULL,
                    last_healthy REAL NOT NULL, provider_errors TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_events(
                    id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT
                );
                CREATE TABLE IF NOT EXISTS supervisor_segments(
                    segment INTEGER PRIMARY KEY, starts REAL NOT NULL, deadline REAL NOT NULL,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_frames(
                    slot INTEGER PRIMARY KEY, at REAL NOT NULL, received REAL NOT NULL,
                    ticker TEXT, healthy INTEGER NOT NULL, complete INTEGER NOT NULL,
                    stale INTEGER NOT NULL, book_state TEXT NOT NULL,
                    reason TEXT, canonical TEXT NOT NULL, sha256 TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_candidates(
                    ticker TEXT PRIMARY KEY, predicted_at REAL NOT NULL, closes_at REAL NOT NULL,
                    probability REAL NOT NULL, evidence_sha256 TEXT NOT NULL,
                    verified_metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_outcomes(
                    ticker TEXT PRIMARY KEY, canonical TEXT NOT NULL,
                    raw_evidence TEXT NOT NULL, received REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_markets(
                    ticker TEXT PRIMARY KEY, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS supervisor_totals(
                    id INTEGER PRIMARY KEY CHECK(id=1), recorded INTEGER NOT NULL,
                    healthy INTEGER NOT NULL, complete INTEGER NOT NULL,
                    one_sided INTEGER NOT NULL, stale INTEGER NOT NULL,
                    discovery_attempts INTEGER NOT NULL, discovery_successes INTEGER NOT NULL,
                    api_errors INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO supervisor_totals VALUES(1,0,0,0,0,0,0,0,0);
            """)
            manifest = canonical({"protocol": json.loads(protocol.canonical()), "policy": POLICY})
            with self.transaction():
                old = self.db.execute("SELECT canonical FROM supervisor_manifest").fetchone()
                if old is not None and old[0] != manifest:
                    raise ValueError("Frozen supervisor policy changed.")
                if old is None:
                    self.db.execute("INSERT INTO supervisor_manifest VALUES(1,?)", (manifest,))
                    self.db.execute(
                        "INSERT INTO supervisor_control VALUES(1,'scheduled',NULL,?,?,?,'{}')",
                        (now, protocol.starts_at, protocol.starts_at),
                    )
                state = self.control()
                if state["status"] in TERMINAL:
                    raise SupervisorStopped("Terminal campaign cannot resume or reset.")
                if now < state["last_wall"]:
                    self._stop("safety_stop", "clock_regressed_on_resume", state["last_wall"])
                else:
                    self.event(
                        "resume" if resume else "created",
                        now,
                        {
                            "deadline": protocol.deadline,
                            "offline_only": True,
                            "previous_checkpoint": state["last_wall"],
                            "unobserved_gap_seconds": max(0, now - state["last_wall"]),
                        },
                    )
        except BaseException:
            if hasattr(self, "registry"):
                self.registry.close()
            self.lock.close()
            raise

    def close(self) -> None:
        self.registry.close()
        self.lock.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def control(self) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM supervisor_control WHERE id=1").fetchone()
        if row is None:
            raise ValueError("Missing supervisor control.")
        return dict(row)

    def _verify_fingerprints(self) -> None:
        model = Path(__file__).with_name("btc_model.py")
        state = ledger_state(self.ledger)
        if (
            state["account_cents"] != (5000, 5000, 0, 0)
            or state["orders"]
            or state["fills"]
            or state["logical_sha256"] != self.protocol.ledger_sha256
            or hashlib.sha256(model.read_bytes()).hexdigest() != self.protocol.model_sha256
            or implementation_sha256() != self.protocol.source_sha256
        ):
            raise ValueError("Frozen implementation/model/$50 ledger mismatch.")

    def event(self, kind: str, now: float, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO supervisor_events VALUES(NULL,?,?,?)", (now, kind, canonical(payload))
        )

    def _stop(self, status: str, reason: str, now: float) -> None:
        if self.control()["status"] in TERMINAL:
            return
        self.db.execute(
            "UPDATE supervisor_control SET status=?,reason=?,last_wall=MAX(last_wall,?) WHERE id=1",
            (status, reason, now),
        )
        self.db.execute("UPDATE supervisor_segments SET state=? WHERE state='active'", (status,))
        self.event(status, now, {"reason": reason})

    def stop(self, *, now: float) -> None:
        if not finite(now) or now <= 0:
            raise ValueError("Invalid stop clock.")
        with self.transaction():
            self._stop("operator_stop", "explicit_operator_stop", now)

    def _flush_forecasts(self, now: float) -> None:
        for row in self.db.execute(
            "SELECT c.* FROM supervisor_candidates c LEFT JOIN campaign_forecasts f "
            "ON f.ticker=c.ticker WHERE f.ticker IS NULL AND c.closes_at-268<?",
            (now,),
        ).fetchall():
            self.registry._forecast_in_transaction(
                row["ticker"],
                row["predicted_at"],
                row["closes_at"],
                row["probability"],
                row["evidence_sha256"],
            )

    def _check(
        self,
        now: float,
        monotonic: float,
        health: HostHealth | None,
    ) -> bool:
        state = self.control()
        if state["status"] in TERMINAL:
            return False
        if (
            not finite(now)
            or not finite(monotonic)
            or monotonic < self.last_mono
            or now < state["last_wall"]
            or abs((now - self.anchor_wall) - (monotonic - self.anchor_mono)) > 2
        ):
            self._stop("safety_stop", "clock_discontinuity", state["last_wall"])
            return False
        self.last_mono = monotonic
        try:
            require_simulation()
            self._verify_fingerprints()
        except (ValueError, OSError, sqlite3.Error, SimulationOnlyError):
            self._stop("safety_stop", "safety_or_frozen_evidence_changed", now)
            return False
        if now >= self.protocol.deadline or monotonic >= self.mono_deadline:
            self._flush_forecasts(self.protocol.deadline)
            self._stop("completed", "original_deadline_reached", now)
            return False
        self.db.execute("UPDATE supervisor_control SET last_wall=? WHERE id=1", (now,))
        if now < self.protocol.starts_at:
            return False
        if health is None or not health.healthy(now):
            self._stop("quality_stop", "host_backup_clock_or_storage_unhealthy", now)
            return False
        # Persisted clocks include downtime and previous segments. Never reset
        # the 60s/180s guards on reconnect or at a six-hour boundary.
        if now - state["last_healthy"] > QUALITY_POLICY["no_healthy_sample_seconds"]:
            self._stop("quality_stop", "no_healthy_sample", now)
            return False
        if now - state["last_verified"] > QUALITY_POLICY["no_verified_market_seconds"]:
            self._stop("quality_stop", "no_verified_market", now)
            return False
        segment = int((now - self.protocol.starts_at) // 21600)
        self.db.execute(
            "UPDATE supervisor_segments SET state='window_elapsed' "
            "WHERE segment<? AND state='active'",
            (segment,),
        )
        exists = self.db.execute(
            "SELECT 1 FROM supervisor_segments WHERE segment=?", (segment,)
        ).fetchone()
        if exists is None:
            starts = self.protocol.starts_at + segment * 21600
            self.db.execute(
                "INSERT INTO supervisor_segments VALUES(?,?,?,'active')",
                (segment, starts, min(starts + 21600, self.protocol.deadline)),
            )
            self.event("segment_opened", now, {"segment": segment})
        self.db.execute("UPDATE supervisor_control SET status='running' WHERE id=1")
        self._flush_forecasts(now)
        return True

    def advance(self, *, now: float, monotonic: float, health: HostHealth | None) -> dict[str, Any]:
        """Advance a synthetic/recorded clock; never polls, sleeps or starts work."""
        with self.transaction():
            self._check(now, monotonic, health)
        return self.report()

    def ingest(
        self,
        frame: Frame,
        *,
        now: float,
        monotonic: float,
        health: HostHealth | None,
    ) -> bool:
        """Record one global two-second slot. Exact retries do nothing.

        Current-slot-only insertion forbids outage backfilling. Forecasts are
        recomputed from supplied proxy ticks using the frozen model, not trusted
        from a caller's probability or a `healthy` field.
        """
        if (
            not finite(frame.at)
            or frame.at < self.protocol.starts_at
            or not isinstance(frame.payload, dict)
            or not isinstance(frame.series, dict)
        ):
            raise ValueError("Invalid frame timestamp.")
        encoded = canonical(
            {
                "at": frame.at,
                "payload": frame.payload,
                "series": frame.series,
                "ticks": [tick.model_dump(mode="json") for tick in frame.ticks],
                "requests": frame.requests,
                "discoveries": frame.discoveries,
            }
        )
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        slot = int((frame.at - self.protocol.starts_at) // 2)
        applied = False
        conflict = False
        with self.transaction():
            if not self._check(now, monotonic, health):
                pass
            else:
                old = self.db.execute(
                    "SELECT sha256 FROM supervisor_frames WHERE slot=?", (slot,)
                ).fetchone()
                if old is not None:
                    if old[0] != fingerprint:
                        self._stop("safety_stop", "conflicting_slot_retry", now)
                        conflict = True
                elif (
                    slot != int((now - self.protocol.starts_at) // 2)
                    or not 0 <= now - frame.at <= 2
                ):
                    raise ValueError("No stale slot insertion or outage backfill.")
                else:
                    self._record(frame, now, slot, encoded, fingerprint)
                    self._flush_forecasts(now)
                    applied = True
        if conflict or self.control()["status"] in TERMINAL:
            raise SupervisorStopped("Supervisor stopped; inspect persistent reason.")
        return applied

    def _record(self, frame: Frame, now: float, slot: int, encoded: str, fingerprint: str) -> None:
        ticker, reason, verified = None, None, None
        try:
            verified = verify_market(frame.payload["market_metadata"], frame.series, now=frame.at)
            ticker = verified.ticker
        except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError):
            reason = "missing_or_unverified_market"
        # Probability-independent data-health check; 0.5 is only an internal
        # freshness probe, never stored as a prediction or calibration evidence.
        execution = executable_edges({**frame.payload, "yes_probability": 0.5}, now)
        healthy = (
            verified is not None
            and execution["strict_execution_eligible"]
            and all(success for _, success in frame.requests)
        )
        if "collector_request_receipt_ids" in frame.payload:
            ids = frame.payload["collector_request_receipt_ids"]
            if (
                not isinstance(ids, list)
                or not 1 <= len(ids) <= 100
                or any(type(i) is not int or i < 1 for i in ids)
                or len(set(ids)) != len(ids)
            ):
                healthy = False
            else:
                placeholders = ",".join("?" for _ in ids)
                rows = self.db.execute(
                    "SELECT COUNT(*) FROM requests r JOIN bridge_requests b ON r.id=b.request_id "
                    f"WHERE r.id IN ({placeholders}) AND r.fresh=1 AND r.error IS NULL "
                    "AND r.received BETWEEN ? AND ?",
                    (*ids, frame.at - 5, now),
                ).fetchone()[0]
                healthy = healthy and rows == len(ids)
        try:
            kind = classify_book(book_fields({"orderbook_fp": frame.payload["raw_book"]}))
        except (ValueError, KeyError, TypeError):
            kind = "unavailable"
        complete = (
            healthy
            and kind == "two_sided"
            and execution["strict_yes_net_edge"] is not None
            and execution["strict_no_net_edge"] is not None
        )
        stale = bool(
            frame.payload.get("quote_timestamp_status") == "invalid_or_stale"
            or frame.payload.get("stale") is True
            or any(
                finite(frame.payload.get(field)) and not 0 <= now - frame.payload[field] <= 5
                for field in (
                    "quote_timestamp",
                    "book_received_at",
                    "reference_received_at",
                    "metadata_received_at",
                )
            )
        )
        if not healthy and reason is None:
            reason = execution["strict_edge_reason"] or "unusable_book"
        if verified is not None:
            identity = canonical(asdict(verified))
            old_market = self.db.execute(
                "SELECT metadata FROM supervisor_markets WHERE ticker=?", (ticker,)
            ).fetchone()
            if old_market is not None and old_market[0] != identity:
                self._stop("safety_stop", "verified_market_identity_changed", now)
                return
            self.db.execute(
                "INSERT OR IGNORE INTO supervisor_markets VALUES(?,?)", (ticker, identity)
            )
            self.db.execute("UPDATE supervisor_control SET last_verified=? WHERE id=1", (now,))
        if healthy:
            self.db.execute("UPDATE supervisor_control SET last_healthy=? WHERE id=1", (now,))
        counters = json.loads(self.control()["provider_errors"])
        for provider, success in frame.requests:
            if provider not in ("kalshi-demo", "coinbase") or type(success) is not bool:
                raise ValueError("Invalid ordered request evidence.")
            counters[provider] = 0 if success else counters.get(provider, 0) + 1
            if counters[provider] >= QUALITY_POLICY["max_consecutive_request_errors"]:
                self._stop("quality_stop", "consecutive_provider_errors", now)
        if any(type(success) is not bool for success in frame.discoveries):
            raise ValueError("Invalid discovery evidence.")
        self.db.execute(
            "UPDATE supervisor_control SET provider_errors=? WHERE id=1", (canonical(counters),)
        )
        self.db.execute(
            "INSERT INTO supervisor_frames VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                slot,
                frame.at,
                now,
                ticker,
                int(healthy),
                int(complete),
                int(stale),
                kind,
                reason,
                encoded,
                fingerprint,
            ),
        )
        self.db.execute(
            "UPDATE supervisor_totals SET recorded=recorded+1,healthy=healthy+?,"
            "complete=complete+?,one_sided=one_sided+?,stale=stale+?,"
            "discovery_attempts=discovery_attempts+?,discovery_successes=discovery_successes+?,"
            "api_errors=api_errors+? WHERE id=1",
            (
                int(healthy),
                int(complete),
                int(kind in ("yes_only", "no_only")),
                int(stale),
                len(frame.discoveries),
                sum(frame.discoveries),
                sum(not success for _, success in frame.requests),
            ),
        )
        if healthy and verified is not None and 270 <= verified.closes_at - frame.at <= 330:
            try:
                forecast = estimate(
                    list(frame.ticks), verified.strike, verified.closes_at, now=frame.at
                )
            except ValueError:
                forecast = None
            if forecast is not None:
                existing = self.db.execute(
                    "SELECT * FROM supervisor_candidates WHERE ticker=?", (ticker,)
                ).fetchone()
                fields = (
                    ticker,
                    frame.at,
                    verified.closes_at,
                    forecast.yes_probability,
                    fingerprint,
                    canonical(asdict(verified)),
                )
                if existing is not None and existing["verified_metadata"] != fields[-1]:
                    self._stop("safety_stop", "verified_market_identity_changed", now)
                elif existing is None or (abs(verified.closes_at - frame.at - 300), frame.at) < (
                    abs(existing["closes_at"] - existing["predicted_at"] - 300),
                    existing["predicted_at"],
                ):
                    frozen = self.db.execute(
                        "SELECT 1 FROM campaign_forecasts WHERE ticker=?", (ticker,)
                    ).fetchone()
                    if frozen is None:
                        self.db.execute(
                            "INSERT OR REPLACE INTO supervisor_candidates VALUES(?,?,?,?,?,?)",
                            fields,
                        )
        window = QUALITY_POLICY["window_slots"]
        if slot >= window - 1:
            rows = self.db.execute(
                "SELECT COALESCE(SUM(healthy),0),COALESCE(SUM(stale),0) "
                "FROM supervisor_frames WHERE slot>? AND slot<=?",
                (slot - window, slot),
            ).fetchone()
            if (window - rows[0]) / window > QUALITY_POLICY["unusable_fraction"]:
                self._stop("quality_stop", "unusable_or_missing_window_fraction", now)
            elif rows[1] / window > QUALITY_POLICY["stale_fraction"]:
                self._stop("quality_stop", "stale_window_fraction", now)

    def settlement(
        self,
        market: dict[str, Any],
        series: dict[str, Any],
        *,
        received_at: float,
        now: float,
        monotonic: float,
        health: HostHealth | None,
    ) -> bool:
        """Only finalized Kalshi evidence, never Coinbase or inferred expiry."""
        applied = False
        with self.transaction():
            if self._check(now, monotonic, health):
                self._flush_forecasts(now)
                verified = verify_market(market, series, now=now, finalized=True)
                candidate = self.db.execute(
                    "SELECT metadata FROM supervisor_markets WHERE ticker=?", (verified.ticker,)
                ).fetchone()
                payout = 1 if market["result"] == "yes" else 0
                if (
                    candidate is None
                    or candidate["metadata"] != canonical(asdict(verified))
                    or not finite(received_at)
                    or not 0 <= now - received_at <= 5
                    or not verified.closes_at
                    <= utc_timestamp(market["settlement_ts"])
                    <= received_at
                    or market["settlement_value_dollars"] != ("1.0000" if payout else "0.0000")
                ):
                    raise ValueError("Unverified/inconsistent finalized evidence.")
                encoded = canonical(
                    {
                        "verified_market": asdict(verified),
                        "outcome": payout,
                        "settled_at": utc_timestamp(market["settlement_ts"]),
                        "payout": market["settlement_value_dollars"],
                    }
                )
                raw = canonical({"market": market, "series": series})
                old = self.db.execute(
                    "SELECT canonical,received FROM supervisor_outcomes WHERE ticker=?",
                    (verified.ticker,),
                ).fetchone()
                if old is not None and old["canonical"] != encoded:
                    self._stop("safety_stop", "conflicting_finalized_outcome", now)
                elif old is None:
                    forecast = self.db.execute(
                        "SELECT 1 FROM campaign_forecasts WHERE ticker=?", (verified.ticker,)
                    ).fetchone()
                    if forecast is not None:
                        self.registry._outcome_in_transaction(
                            verified.ticker, payout, received_at, sha(json.loads(raw))
                        )
                    self.db.execute(
                        "INSERT INTO supervisor_outcomes VALUES(?,?,?,?)",
                        (verified.ticker, encoded, raw, received_at),
                    )
                    self.event("finalized_outcome", now, {"ticker": verified.ticker})
                    applied = True
        if self.control()["status"] in TERMINAL:
            raise SupervisorStopped("Supervisor stopped; no new outcome polling permitted.")
        return applied

    def report(self) -> dict[str, Any]:
        state = self.control()
        elapsed = max(0, min(state["last_wall"], self.protocol.deadline) - self.protocol.starts_at)
        expected = (
            min(
                math.ceil((self.protocol.deadline - self.protocol.starts_at) / 2),
                int(elapsed // 2) + 1,
            )
            if state["last_wall"] >= self.protocol.starts_at
            else 0
        )
        rows = self.db.execute("SELECT * FROM supervisor_totals WHERE id=1").fetchone()
        counters = {
            "attempts": rows["discovery_attempts"],
            "successes": rows["discovery_successes"],
            "api_errors": rows["api_errors"],
        }
        score_counts = {
            cohort: self.db.execute(
                "SELECT COUNT(*) FROM campaign_forecasts WHERE cohort=? AND outcome IS NOT NULL "
                "AND (cohort='test' OR confirmed_at<?)",
                (cohort, self.protocol.test_starts_at),
            ).fetchone()[0]
            for cohort in ("development", "selection", "test")
        }
        return {
            "status": state["status"],
            "stop_reason": state["reason"],
            "original_deadline": self.protocol.deadline,
            "offline_only": True,
            "expected_slots_through_checkpoint": expected,
            "recorded_slots": rows["recorded"],
            "valid_observations": rows["complete"],
            "usable_observations_including_one_sided": rows["healthy"],
            "incomplete_observations": rows["recorded"] - rows["complete"],
            "one_sided_observations": rows["one_sided"],
            "unrecorded_slots": max(0, expected - rows["recorded"]),
            "missing_or_incomplete_percentage": (
                100 * max(0, expected - rows["complete"]) / expected if expected else None
            ),
            "stale_observations": rows["stale"],
            "api_errors": rows["api_errors"],
            "discovery": {
                **counters,
                "success_rate": (
                    counters["successes"] / counters["attempts"] if counters["attempts"] else None
                ),
            },
            "resumes": self.db.execute(
                "SELECT COUNT(*) FROM supervisor_events WHERE kind='resume'"
            ).fetchone()[0],
            **self.registry.readiness(),
            "verified_distinct_markets": self.db.execute(
                "SELECT COUNT(*) FROM supervisor_markets"
            ).fetchone()[0],
            "finalized_verified_markets": self.db.execute(
                "SELECT COUNT(*) FROM supervisor_outcomes"
            ).fetchone()[0],
            "segments": [
                dict(row)
                for row in self.db.execute("SELECT * FROM supervisor_segments ORDER BY segment")
            ],
            "score_eligible_distinct_markets": score_counts,
            "minimum_sample_counts_met": all(
                score_counts[c] >= n
                for c, n in (("development", 100), ("selection", 100), ("test", 200))
            ),
            "collection_started": False,
            "entries_enabled": False,
            "live_enabled": False,
            "calibration_ready": False,
            "data_supports_model_validation": False,
            "limitations": [
                "Offline inputs and host facts are not authenticated provider observations.",
                "Demo collector bridge is sealed; no daemon/service or collection authorization.",
                "No model fitting, test-score display, calibration promotion or orders.",
                "Distinct contracts are not proof of statistical independence.",
            ],
        }


def run_collection() -> None:
    """Deliberate release boundary: no environment variable can launch collection."""
    require_simulation()
    raise SimulationOnlyError("Offline supervisor only; collection release and approval required.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect existing offline supervisor state only.")
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    require_simulation()
    db = connect_readonly(args.database)
    try:
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM supervisor_control WHERE id=1").fetchone()
        if row is None:
            raise ValueError("No supervisor state.")
        print(
            canonical(
                {
                    **dict(row),
                    "offline_only": True,
                    "collection_started": False,
                    "entries_enabled": False,
                    "live_enabled": False,
                }
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
