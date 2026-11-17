"""Restart-safe, bounded public-demo observer. No broker and no orders.

This module is deliberately separate from strategy execution. A run has one
immutable wall-clock deadline, a single-writer OS lock, and durable time slots.
Resuming never backfills an outage or extends the authorized six-hour window.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import signal
import sqlite3
import time
from collections import Counter
from contextlib import suppress
from dataclasses import asdict
from importlib import import_module
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote

import httpx

from kalshi_bot.btc_model import ReferenceTick
from kalshi_bot.btc_observe import Observer, book_fields, ledger_state
from kalshi_bot.btc_rules import SERIES, VerifiedMarket, utc_timestamp, verify_market, verify_series
from kalshi_bot.btc_validate import executable_edges
from kalshi_bot.exchange.client import API_PREFIX, KalshiClient
from kalshi_bot.safety import require_collection_release, require_simulation

VERSION = 2
MAX_SECONDS = 21600
QUALITY_POLICY = {
    "window_slots": 150,
    "unusable_fraction": 0.40,
    "stale_fraction": 0.10,
    "no_verified_market_seconds": 180,
    "no_healthy_sample_seconds": 60,
    "max_consecutive_request_errors": 10,
    "minimum_free_bytes": 250_000_000,
}


class QualityStop(RuntimeError):
    """Data-only circuit breaker, never converted to a transient discovery skip."""


def implementation_fingerprint() -> str:
    root = Path(__file__).parent
    files = (
        "btc_collect.py",
        "btc_observe.py",
        "btc_model.py",
        "btc_rules.py",
        "btc_validate.py",
        "safety.py",
        "exchange/client.py",
    )
    return hashlib.sha256(b"".join((root / name).read_bytes() for name in files)).hexdigest()


def quote_timestamp(body: dict[str, Any], received: float) -> dict[str, Any]:
    """Only explicitly quote/book timestamps; metadata.updated_time is NOT one."""
    stamps = []
    for container, obj in (("root", body), ("orderbook_fp", body.get("orderbook_fp", {}))):
        if not isinstance(obj, dict):
            continue
        for key in ("quote_timestamp", "quote_ts", "orderbook_timestamp", "orderbook_ts"):
            if key not in obj:
                continue
            raw = obj[key]
            try:
                stamp = utc_timestamp(raw) if isinstance(raw, str) else float(raw)
                age = received - stamp
                valid = not isinstance(raw, bool) and math.isfinite(stamp) and 0 <= age <= 5
            except (ValueError, TypeError, OverflowError):
                stamp, age, valid = None, None, False
            stamps.append(
                {
                    "quote_timestamp_raw": raw,
                    "quote_timestamp_field": container + "." + key,
                    "quote_timestamp": stamp,
                    "quote_age_s": age,
                    "quote_timestamp_status": "fresh" if valid else "invalid_or_stale",
                }
            )
    if stamps:
        first = stamps[0]
        # Conflicting event fields must not let an earlier fresh value mask an
        # invalid/stale second one. Never choose the most convenient timestamp.
        if any(
            s["quote_timestamp_status"] != "fresh"
            or s["quote_timestamp"] != first["quote_timestamp"]
            for s in stamps
        ):
            return {
                **first,
                "quote_timestamp_status": "invalid_or_stale",
                "quote_timestamp_conflict": len(stamps) > 1,
            }
        return first
    return {
        "quote_timestamp": None,
        "quote_age_s": None,
        "quote_timestamp_status": "unavailable",
        "quote_timestamp_field": None,
    }


def classify_book(payload: dict[str, Any]) -> str:
    if "raw_book" not in payload:
        return "unavailable"
    yes, no = payload.get("yes_bid"), payload.get("no_bid")
    if yes is None and no is None:
        return "empty"
    if yes is None:
        return "no_only"
    if no is None:
        return "yes_only"
    return "two_sided"


def current_candidates(
    candidates: dict[str, dict[str, Any]], now: float
) -> list[tuple[str, dict[str, Any]]]:
    """Filter before applying request caps; cached names are never synthesized."""
    result = []
    for ticker, raw in candidates.items():
        try:
            opens, closes = utc_timestamp(raw["open_time"]), utc_timestamp(raw["close_time"])
            if (
                ticker == raw["ticker"]
                and raw.get("status") in ("active", "open")
                and str(raw["event_ticker"]).startswith(SERIES + "-")
                and ticker.startswith(str(raw["event_ticker"]) + "-")
                and opens <= now < closes
                and closes - opens == 900
                and opens % 900 == 0
            ):
                result.append((ticker, raw))
        except (ValueError, KeyError, TypeError, OverflowError):
            continue
    return sorted(result, key=lambda item: (utc_timestamp(item[1]["close_time"]), item[0]))


def _lock(path: Path) -> BinaryIO:
    """Nonblocking kernel lock. A killed process releases it without stale leases."""
    handle = path.open("a+b")
    try:
        if os.name == "nt":
            msvcrt = import_module("msvcrt")

            handle.seek(0)
            if not handle.read(1):
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        handle.close()
        raise
    return handle


class Collector(Observer):
    def __init__(
        self,
        output: Path,
        ledger: Path,
        *,
        seconds: float = MAX_SECONDS,
        interval: float = 2,
        resume: bool = False,
    ) -> None:
        require_simulation()
        if not 0 < seconds <= MAX_SECONDS or not 1 <= interval <= 5:
            raise ValueError("Only <=6 hours at 1-5 seconds; no live or entry mode exists.")
        if output.resolve() == ledger.resolve():
            raise ValueError("Observation database cannot be the paper account.")
        before = ledger_state(ledger)
        if before["account_cents"] != (5000, 5000, 0, 0) or before["orders"] or before["fills"]:
            raise ValueError("Refusing collection: expected unchanged $50 account and no orders.")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.lock = _lock(Path(str(output) + ".lock"))
        self.ledger = ledger
        self.interval = interval
        self.slot = -1
        self.failures = 0
        self.request_errors: dict[str, int] = {}
        self.current_quotes: dict[str, dict[str, Any]] = {}
        self.candidates: dict[str, dict[str, Any]] = {}
        self.next_listing = 0.0
        self.stop_reason: str | None = None
        try:
            if resume:
                if not output.exists():
                    raise ValueError("Resume requires an existing version-2 database.")
                self.output = output
                self.db = sqlite3.connect(output, timeout=5)
                self.markets = {}
                self.series = {}
                self.active = []
                self.ticks = []
                self.next_discovery = self.next_settlement = 0.0
                manifest = self.db.execute("SELECT value FROM run WHERE key='manifest'").fetchone()
                if manifest is None:
                    raise ValueError("Not a resumable collector database; preserve raw evidence.")
                self.manifest = json.loads(manifest[0])
                if (
                    self.manifest["version"] != VERSION
                    or self.manifest["seconds"] != seconds
                    or self.manifest["interval"] != interval
                    or self.manifest["quality_policy"] != QUALITY_POLICY
                    or self.manifest["implementation_fingerprint"] != implementation_fingerprint()
                    or self.manifest["ledger_before"]["logical_sha256"] != before["logical_sha256"]
                ):
                    raise ValueError("Resume configuration or ledger mismatch.")
                status = self.db.execute("SELECT value FROM run WHERE key='status'").fetchone()
                if status and json.loads(status[0]) in ("completed", "quality_stop", "safety_stop"):
                    raise ValueError("Terminal run cannot resume; new authorization required.")
                if time.time() >= self.manifest["deadline"]:
                    raise ValueError("Authorized deadline expired; no automatic extension.")
                for ticker, metadata in self.db.execute("SELECT ticker,metadata FROM markets"):
                    self.markets[ticker] = VerifiedMarket(**json.loads(metadata))
                # Reference continuity must be re-established after interruptions.
                # Old features/forecasts are retained, never trained on or relabeled.
            else:
                super().__init__(output)
                self.initialize_collector_tables(self.db)
                now = time.time()
                self.manifest = {
                    "version": VERSION,
                    "started": now,
                    "deadline": now + seconds,
                    "seconds": seconds,
                    "interval": interval,
                    "environment": "demo",
                    "entries_enabled": False,
                    "ledger_before": before,
                    "reference_source": "coinbase:BTC-USD (proxy, NOT official settlement)",
                    "future_max_risk_cents": 100,
                    "future_min_net_edge": 0.08,
                    "quality_policy": QUALITY_POLICY,
                    "implementation_fingerprint": implementation_fingerprint(),
                }
                self.set_run("manifest", self.manifest)
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS request_parameters "
                "(request_id INTEGER PRIMARY KEY REFERENCES requests(id), parameters TEXT NOT NULL)"
            )
            self.db.commit()
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA busy_timeout=5000")
            self.db.execute("PRAGMA wal_autocheckpoint=256")
            check = self.db.execute("PRAGMA quick_check").fetchone()
            if check != ("ok",):
                raise ValueError("SQLite integrity check failed.")
            self.last_verified = float(self.manifest["started"])
            self.last_healthy = float(self.manifest["started"])
            for key, value in self.db.execute(
                "SELECT key,value FROM run WHERE key IN ('last_verified','last_healthy')"
            ):
                if key == "last_verified":
                    self.last_verified = float(json.loads(value))
                else:
                    self.last_healthy = float(json.loads(value))
            self.db.execute("UPDATE slots SET state='interrupted' WHERE finished IS NULL")
            self.event(
                "resume" if resume else "start",
                {
                    "pid": os.getpid(),
                    "deadline": self.manifest["deadline"],
                    "reference_warmup_reset": True,
                },
            )
            self.set_run("status", "running")
            self.db.commit()
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise

    @staticmethod
    def initialize_collector_tables(db: sqlite3.Connection) -> None:
        db.executescript("""
                    CREATE TABLE IF NOT EXISTS slots (
                        slot INTEGER PRIMARY KEY, started REAL, finished REAL,
                        healthy INTEGER, state TEXT
                    );
                    CREATE TABLE IF NOT EXISTS references_seen (
                        slot INTEGER PRIMARY KEY, received REAL,
                        trade_at REAL, price REAL, valid INTEGER, error TEXT, payload TEXT
                    );
                    CREATE TABLE IF NOT EXISTS candidate_books (
                        slot INTEGER, ticker TEXT, received REAL, payload TEXT,
                        PRIMARY KEY(slot,ticker)
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        id INTEGER PRIMARY KEY, at REAL, kind TEXT, payload TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS observation_slot
                    ON observations(json_extract(payload,'$.slot'),COALESCE(ticker,''));
                """)

    def set_run(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO run VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO events VALUES(NULL,?,?,?)", (time.time(), kind, json.dumps(payload))
        )

    async def get(
        self,
        client: httpx.AsyncClient,
        provider: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = await super().get(client, provider, path, params)
        self.db.execute(
            "INSERT INTO request_parameters VALUES(last_insert_rowid(),?)",
            (json.dumps(params or {}, sort_keys=True),),
        )
        self.db.commit()
        self.request_errors[provider] = (
            self.request_errors.get(provider, 0) + 1 if result["error"] else 0
        )
        if self.request_errors[provider] >= QUALITY_POLICY["max_consecutive_request_errors"]:
            raise QualityStop("quality_stop: consecutive public API request failures")
        if path.endswith("/orderbook"):
            ticker = path.split("/")[-2]
            self.current_quotes[ticker] = quote_timestamp(result["body"], result["received"])
        return result

    def reference(self, response: dict[str, Any]) -> tuple[ReferenceTick | None, str | None]:
        tick, error = super().reference(response)
        # Trim by time as well as count, avoiding permanently failed warmup after 30 min.
        self.ticks = [t for t in self.ticks if t.timestamp >= time.time() - 1799]
        self.db.execute(
            "INSERT OR IGNORE INTO references_seen VALUES(?,?,?,?,?,?,?)",
            (
                self.slot,
                response["received"],
                tick.timestamp if tick else None,
                tick.price if tick else None,
                int(tick is not None),
                error,
                json.dumps({"source": self.manifest["reference_source"], **response["body"]}),
            ),
        )
        self.db.commit()
        return tick, error

    async def discover(self, client: httpx.AsyncClient) -> None:
        if time.monotonic() < self.next_discovery:
            return
        rejected: list[dict[str, Any]] = []
        active: dict[str, VerifiedMarket] = {}
        failure = None
        try:
            response = await self.get(client, "kalshi-demo", API_PREFIX + "/series/" + SERIES)
            if not response["fresh"]:
                raise ValueError("Series unavailable or stale.")
            self.series = response["body"]["series"]
            verify_series(self.series)
            cached = bool(current_candidates(self.candidates, time.time())) and (
                time.monotonic() < self.next_listing
            )
            cursor, seen = "", set()
            raw_candidates: dict[str, dict[str, Any]] = dict(self.candidates) if cached else {}
            for _ in range(0 if cached else 10):
                page = await self.get(
                    client,
                    "kalshi-demo",
                    API_PREFIX + "/markets",
                    {
                        "series_ticker": SERIES,
                        "limit": 100,
                        "min_close_ts": int(time.time()),
                        "max_close_ts": int(time.time()) + 1800,
                        "cursor": cursor,
                    },
                )
                if not page["fresh"]:
                    raise ValueError("Discovery list unavailable or stale.")
                for raw in page["body"]["markets"]:
                    if not str(raw.get("event_ticker", "")).startswith(SERIES + "-"):
                        continue
                    raw_candidates[str(raw["ticker"])] = raw
                cursor = page["body"]["cursor"]
                if not cursor:
                    break
                if cursor in seen:
                    raise ValueError("Discovery pagination loop.")
                seen.add(cursor)
            else:
                if not cached:
                    raise ValueError("Discovery pagination exceeded bound.")
            if not cached:
                self.next_listing = time.monotonic() + 30
            # Keep actual provider candidates only, never synthesize a ticker.
            self.candidates = raw_candidates
            details = 0
            # Apply current, aligned interval / actual series identity checks
            # BEFORE detail bounds, not just before quarantined-book requests.
            # Invalid intervals must not consume the eight-detail rollover cap.
            for ticker, raw in current_candidates(raw_candidates, time.time()):
                try:
                    opens = utc_timestamp(raw["open_time"])
                    closes = utc_timestamp(raw["close_time"])
                    if not opens <= time.time() < closes:
                        rejected.append(
                            {"ticker": ticker, "stage": "timing", "reason": "not_currently_open"}
                        )
                        continue
                    try:
                        if cached:
                            raise ValueError("Cached candidate requires fresh detail verification.")
                        market = verify_market(raw, self.series, now=time.time())
                    except (ValueError, KeyError, TypeError):
                        if details >= 8:
                            raise ValueError("Candidate detail bound reached.") from None
                        details += 1
                        detail = await self.get(
                            client,
                            "kalshi-demo",
                            API_PREFIX + "/markets/" + quote(ticker, safe=""),
                        )
                        if not detail["fresh"]:
                            raise ValueError("Candidate detail unavailable or stale.") from None
                        raw = detail["body"]["market"]
                        if raw.get("ticker") != ticker:
                            raise ValueError("Candidate detail ticker mismatch.") from None
                        self.candidates[ticker] = raw
                        market = verify_market(raw, self.series, now=time.time())
                    old = self.markets.get(ticker)
                    if old is not None and old != market:
                        raise ValueError("Verified market identity changed; refusing replacement.")
                    active[ticker] = market
                    self.markets[ticker] = market
                    self.db.execute(
                        "INSERT OR IGNORE INTO markets VALUES(?,?,?,NULL,NULL,NULL)",
                        (ticker, time.time(), json.dumps(asdict(market))),
                    )
                except (ValueError, KeyError, TypeError) as exc:
                    rejected.append(
                        {
                            "ticker": ticker,
                            "stage": "verification",
                            "reason": str(exc),
                            "status": raw.get("status"),
                            "floor_strike": raw.get("floor_strike"),
                            "updated_time": raw.get("updated_time"),
                        }
                    )
        except (ValueError, KeyError, TypeError) as exc:
            failure = str(exc)
            active = {}
        self.active = list(active.values())
        now = time.time()
        if active:
            self.failures = 0
            self.last_verified = now
            self.set_run("last_verified", now)
        else:
            self.failures += 1
        delay = min(10, 2 ** min(self.failures, 4)) if not active else 30
        if active:
            delay = min(delay, max(0.25, min(m.closes_at for m in active.values()) - now))
            if any(m.closes_at - now <= 30 for m in active.values()):
                delay = min(delay, 5)
        self.next_discovery = time.monotonic() + delay
        self.db.execute(
            "INSERT INTO discovery VALUES(?,?,?,?,?)",
            (
                now,
                int(bool(active) and failure is None),
                len(active),
                json.dumps(rejected),
                failure,
            ),
        )
        self.db.commit()

    def record_observation(
        self,
        at: float,
        ticker: str | None,
        valid: bool,
        missing: bool,
        stale: bool,
        payload: dict[str, Any],
    ) -> None:
        raw = payload.get("raw_book")
        times = self.current_quotes.get(ticker or "", quote_timestamp({}, at))
        payload.update(
            {
                "slot": self.slot,
                "reference_source": self.manifest["reference_source"],
                "official_settlement_price": None,
                "book_state": classify_book(payload),
                "market_updated_time": payload.get("market_metadata", {}).get("updated_time"),
                **times,
            }
        )
        if times["quote_timestamp_status"] == "invalid_or_stale":
            stale, valid = True, False
            payload.setdefault("errors", []).append("Explicit quote timestamp invalid/stale.")
            for key in (
                "yes_net_edge",
                "no_net_edge",
                "minimum_haircut_yes_edge",
                "minimum_haircut_no_edge",
            ):
                payload[key] = None
            payload["paper_quote_eligible"] = False
        if times["quote_timestamp_status"] != "fresh":
            payload["paper_quote_eligible"] = False
        payload.update(executable_edges(payload, at))
        payload["legacy_edges_diagnostic_only"] = True
        payload["transport_and_reference_valid"] = valid
        # Valid provider/reference + one-sided book is useful observation data,
        # but is NEVER a complete or executable two-sided quote.
        partial = bool(
            raw is not None
            and payload.get("reference_valid")
            and not stale
            and payload["book_state"] in ("yes_only", "no_only")
            and payload.get("errors") == ["One-sided book; complete executable spread unavailable."]
        )
        payload["data_healthy"] = bool(valid or partial)
        if payload["data_healthy"]:
            self.last_healthy = at
            self.set_run("last_healthy", at)
        self.db.execute(
            "INSERT OR IGNORE INTO observations VALUES(NULL,?,?,?,?,?,?)",
            (at, ticker, int(valid), int(missing), int(stale), json.dumps(payload)),
        )

    async def cycle(self, kalshi: httpx.AsyncClient, coinbase: httpx.AsyncClient) -> None:
        await super().cycle(kalshi, coinbase)
        if not any(m.opens_at <= time.time() < m.closes_at for m in self.active):
            for ticker, metadata in current_candidates(self.candidates, time.time())[:8]:
                result = await self.get(
                    kalshi,
                    "kalshi-demo",
                    API_PREFIX + "/markets/" + quote(ticker, safe="") + "/orderbook",
                )
                payload = {
                    "metadata": metadata,
                    "verified": False,
                    "strike_for_model": None,
                    "entries_enabled": False,
                    "reference_source": self.manifest["reference_source"],
                    "http_fresh": result["fresh"],
                    "error": result["error"],
                    "raw_response": result["body"],
                    **quote_timestamp(result["body"], result["received"]),
                }
                with suppress(ValueError, KeyError, TypeError, ArithmeticError):
                    payload.update(book_fields(result["body"]))
                payload["book_state"] = classify_book(payload)
                self.db.execute(
                    "INSERT OR IGNORE INTO candidate_books VALUES(?,?,?,?)",
                    (self.slot, ticker, result["received"], json.dumps(payload)),
                )
            self.db.commit()

    def quality_reason(self, now: float) -> str | None:
        if now < self.manifest["started"] - 1:
            return "safety_stop: wall clock moved before run start"
        if (
            ledger_state(self.ledger)["logical_sha256"]
            != self.manifest["ledger_before"]["logical_sha256"]
        ):
            return "safety_stop: paper account changed externally"
        if now - self.last_verified > QUALITY_POLICY["no_verified_market_seconds"]:
            return "quality_stop: no verified active market for 180 seconds"
        # Expected rollover metadata lag has its own 180-second allowance.
        if now - self.last_healthy > QUALITY_POLICY["no_healthy_sample_seconds"] and any(
            m.opens_at <= now < m.closes_at for m in self.active
        ):
            return "quality_stop: no healthy verified-market data for 60 seconds"
        if shutil.disk_usage(self.output.parent).free < QUALITY_POLICY["minimum_free_bytes"]:
            return "quality_stop: less than 250 MB free disk"
        recent = self.db.execute(
            "SELECT healthy FROM slots WHERE finished IS NOT NULL ORDER BY slot DESC LIMIT ?",
            (QUALITY_POLICY["window_slots"],),
        ).fetchall()
        if (
            len(recent) == QUALITY_POLICY["window_slots"]
            and sum(not r[0] for r in recent) / len(recent) > QUALITY_POLICY["unusable_fraction"]
        ):
            return "quality_stop: more than 40% unusable sampling slots in rolling window"
        row = self.db.execute(
            "SELECT count(*),COALESCE(sum(stale),0) FROM observations "
            "WHERE json_extract(payload,'$.slot')>=?",
            (max(0, self.slot - QUALITY_POLICY["window_slots"] + 1),),
        ).fetchone()
        if row[0] >= 30 and row[1] / row[0] > QUALITY_POLICY["stale_fraction"]:
            return "quality_stop: more than 10% stale market snapshots"
        return None

    def summary(self, status: str) -> dict[str, Any]:
        after = ledger_state(self.ledger)
        rows = self.db.execute("SELECT valid,missing,stale,payload FROM observations").fetchall()
        states = Counter(json.loads(r[3]).get("book_state") for r in rows)
        request_count, errors = self.db.execute(
            "SELECT count(*),COALESCE(sum(error IS NOT NULL),0) FROM requests"
        ).fetchone()
        report = {
            "status": status,
            "stop_reason": self.stop_reason,
            "manifest": self.manifest,
            "finished": time.time(),
            "entries_enabled": False,
            "promotion_allowed": False,
            "market_count": len(self.markets),
            "observations": len(rows),
            "valid_observations": sum(r[0] for r in rows),
            "missing_observations": sum(r[1] for r in rows),
            "stale_observations": sum(r[2] for r in rows),
            "book_states": dict(states),
            "quarantined_candidate_books": self.db.execute(
                "SELECT count(*) FROM candidate_books"
            ).fetchone()[0],
            "api_requests": request_count,
            "api_errors": errors,
            "finalized_markets": self.db.execute(
                "SELECT count(*) FROM markets WHERE outcome IS NOT NULL"
            ).fetchone()[0],
            "paper_ledger_after": after,
            "paper_ledger_unchanged": after["logical_sha256"]
            == self.manifest["ledger_before"]["logical_sha256"],
            "calibration_ready": False,
        }
        expected_slots = min(
            math.ceil(self.manifest["seconds"] / self.interval),
            max(
                0,
                math.ceil(
                    (min(time.time(), self.manifest["deadline"]) - self.manifest["started"])
                    / self.interval
                ),
            ),
        )
        slot_count, completed_slots, healthy_slots = self.db.execute(
            "SELECT count(*),COALESCE(sum(finished IS NOT NULL),0),"
            "COALESCE(sum(healthy),0) FROM slots"
        ).fetchone()
        discovery = self.db.execute(
            "SELECT count(*),COALESCE(sum(success),0) FROM discovery"
        ).fetchone()
        report.update(
            {
                "expected_slots_elapsed": expected_slots,
                "attempted_slots": slot_count,
                "completed_slots": completed_slots,
                "healthy_slots": healthy_slots,
                "unattempted_slots": max(0, expected_slots - slot_count),
                "unhealthy_or_missing_slots": max(0, expected_slots - healthy_slots),
                "discovery_attempts": discovery[0],
                "discovery_successes": discovery[1],
                "resume_count": self.db.execute(
                    "SELECT count(*) FROM events WHERE kind='resume'"
                ).fetchone()[0],
                "quote_timestamp_statuses": dict(
                    Counter(json.loads(r[3]).get("quote_timestamp_status") for r in rows)
                ),
                "settlement_policy": "Verified finalized demo labels received before deadline.",
            }
        )
        self.set_run("summary", report)
        self.set_run("status", status)
        self.event("stop", {"status": status, "reason": self.stop_reason})
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
        return report

    def close(self) -> None:
        self.db.close()
        self.lock.close()


async def collect(
    output: Path,
    ledger: Path,
    *,
    seconds: float = MAX_SECONDS,
    interval: float = 2,
    resume: bool = False,
) -> dict[str, Any]:
    require_collection_release()
    observer = Collector(output, ledger, seconds=seconds, interval=interval, resume=resume)
    status = "interrupted"
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            registered.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    # Monotonic cap prevents clock rollback extending a process; persisted UTC
    # deadline prevents restart from extending its authorized wall-clock window.
    monotonic_deadline = time.monotonic() + max(0, observer.manifest["deadline"] - time.time())
    try:
        async with (
            KalshiClient("https://demo-api.kalshi.co", timeout=2) as kalshi,
            httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False) as coinbase,
        ):
            while not stop.is_set():
                now = time.time()
                remaining = min(
                    observer.manifest["deadline"] - now,
                    monotonic_deadline - time.monotonic(),
                )
                if remaining <= 0:
                    status = "completed"
                    break
                observer.slot = int((now - observer.manifest["started"]) // interval)
                reason = observer.quality_reason(now)
                if reason:
                    observer.stop_reason = reason
                    status = reason.split(":")[0]
                    break
                inserted = observer.db.execute(
                    "INSERT OR IGNORE INTO slots VALUES(?,?,NULL,0,'started')",
                    (observer.slot, now),
                ).rowcount
                observer.db.commit()
                if inserted:
                    try:
                        observer.current_quotes.clear()
                        await asyncio.wait_for(
                            observer.cycle(kalshi._client, coinbase),
                            timeout=remaining,
                        )
                    except TimeoutError:
                        status = "completed"
                        break
                    healthy = observer.db.execute(
                        "SELECT COALESCE(max(json_extract(payload,'$.data_healthy')),0) "
                        "FROM observations WHERE json_extract(payload,'$.slot')=?",
                        (observer.slot,),
                    ).fetchone()[0]
                    observer.db.execute(
                        "UPDATE slots SET finished=?,healthy=?,state='finished' WHERE slot=?",
                        (time.time(), healthy, observer.slot),
                    )
                    observer.set_run(
                        "heartbeat",
                        {
                            "at": time.time(),
                            "slot": observer.slot,
                            "deadline": observer.manifest["deadline"],
                        },
                    )
                    observer.db.commit()
                wait = min(
                    observer.manifest["started"] + (observer.slot + 1) * interval - time.time(),
                    monotonic_deadline - time.monotonic(),
                )
                if wait > 0:
                    with suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=wait)
            if stop.is_set():
                observer.stop_reason = "interrupted: signal; explicit resume required"
    except asyncio.CancelledError:
        observer.stop_reason = "interrupted: cancelled; explicit resume required"
    except Exception as exc:
        observer.stop_reason = (
            str(exc)
            if str(exc).startswith("quality_stop:")
            else ("interrupted: " + type(exc).__name__)
        )
        status = (
            "quality_stop" if observer.stop_reason.startswith("quality_stop:") else "interrupted"
        )
    finally:
        for sig in registered:
            loop.remove_signal_handler(sig)
    try:
        return observer.summary(status)
    finally:
        observer.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=Path("data/paper.db"))
    parser.add_argument("--seconds", type=float, default=MAX_SECONDS)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    result = asyncio.run(
        collect(
            args.output,
            args.ledger,
            seconds=args.seconds,
            interval=args.interval,
            resume=args.resume,
        )
    )
    print(json.dumps(result, indent=2), flush=True)
    raise SystemExit(0 if result["status"] == "completed" else 2)


if __name__ == "__main__":
    main()
