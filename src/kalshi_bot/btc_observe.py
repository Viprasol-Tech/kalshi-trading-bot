"""Bounded observation-only capture. Never constructs a broker or an order.

All timestamps are UTC epoch seconds; prices are dollars and depth is contracts.
The paper ledger is opened read-only solely to verify that it did not change.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import math
import sqlite3
import time
from collections import Counter
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from kalshi_bot.btc_data import CoinbaseReference, orderbook_quote
from kalshi_bot.btc_model import ReferenceTick, estimate
from kalshi_bot.btc_rules import (
    SERIES,
    VerifiedMarket,
    utc_timestamp,
    verify_market,
    verify_series,
)
from kalshi_bot.exchange.client import API_PREFIX, KalshiClient
from kalshi_bot.safety import require_collection_release, require_simulation


def ledger_state(path: Path) -> dict[str, Any]:
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        tables = db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        ).fetchall()
        contents = {}
        for (name,) in tables:
            escaped = name.replace('"', '""')
            contents[name] = db.execute(f'SELECT * FROM "{escaped}" ORDER BY rowid').fetchall()
        account = db.execute(
            "SELECT initial_cash,cash,realized_pnl,fees FROM account WHERE id=1"
        ).fetchone()
        return {
            "logical_sha256": hashlib.sha256(
                json.dumps(contents, sort_keys=True).encode()
            ).hexdigest(),
            "account_cents": account,
            "orders": len(contents["orders"]),
            "fills": len(contents["fills"]),
        }


def book_fields(body: dict[str, Any]) -> dict[str, Any]:
    """Preserve sub-cent prices/fractional sizes; do not invent executable depth."""
    book = body["orderbook_fp"]
    sides = {}
    for side in ("yes", "no"):
        levels = []
        for price, size in book[side + "_dollars"]:
            p, quantity = Decimal(price), Decimal(size)
            if (
                not p.is_finite()
                or not quantity.is_finite()
                or not 0 < p < 1
                or not 0 <= quantity <= 1e12
            ):
                raise ValueError("Invalid raw price or depth.")
            if quantity > 0:
                levels.append((float(p), float(quantity)))
        sides[side] = sorted(levels, reverse=True)
    y = sides["yes"][0][0] if sides["yes"] else None
    n = sides["no"][0][0] if sides["no"] else None
    if y is not None and n is not None and y + n > 1 + 1e-12:
        raise ValueError("Crossed raw complementary book.")
    return {
        "yes_bid": y,
        "yes_ask": 1 - n if n is not None else None,
        "no_bid": n,
        "no_ask": 1 - y if y is not None else None,
        "yes_bid_depth": sum(q for p, q in sides["yes"] if p == y),
        "yes_ask_depth": sum(q for p, q in sides["no"] if p == n),
        "no_bid_depth": sum(q for p, q in sides["no"] if p == n),
        "no_ask_depth": sum(q for p, q in sides["yes"] if p == y),
        "yes_bid_ladder": sides["yes"],
        "no_bid_ladder": sides["no"],
        "raw_book": book,
    }


class Observer:
    """Data-only HTTP and SQLite, with no trading/ledger mutation capability."""

    def __init__(self, output: Path) -> None:
        if output.exists():
            raise ValueError("Use a new observation database; never overwrite a run.")
        output.parent.mkdir(parents=True, exist_ok=True)
        self.output = output
        self.db = sqlite3.connect(output)
        self.initialize_tables(self.db)
        self.initialize_state()

    @staticmethod
    def initialize_tables(db: sqlite3.Connection) -> None:
        """Shared raw evidence schema; also used in the campaign's single DB."""
        db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS run (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, started REAL, received REAL, provider TEXT,
                path TEXT, latency_ms REAL, status INTEGER, server_age_s REAL,
                fresh INTEGER, error TEXT, body TEXT
            );
            CREATE TABLE IF NOT EXISTS discovery (
                at REAL, success INTEGER, active INTEGER, rejected TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS markets (
                ticker TEXT PRIMARY KEY, first_seen REAL, metadata TEXT,
                outcome INTEGER, confirmed_at REAL, final_metadata TEXT
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, at REAL, ticker TEXT, valid INTEGER,
                missing INTEGER, stale INTEGER, payload TEXT
            );
        """)

    def initialize_state(self) -> None:
        self.markets: dict[str, VerifiedMarket] = {}
        self.series: dict[str, Any] = {}
        self.active: list[VerifiedMarket] = []
        self.ticks: list[ReferenceTick] = []
        self.next_discovery = 0.0
        self.next_settlement = 0.0

    async def get(
        self,
        client: httpx.AsyncClient,
        provider: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_simulation()
        started = time.time()
        result: dict[str, Any] = {
            "body": {},
            "status": None,
            "fresh": False,
            "error": None,
            "server_age_s": None,
        }
        cancelled = False
        try:
            response = await client.get(path, params=params, headers={"Cache-Control": "no-cache"})
            result["status"] = response.status_code
            result["body"] = response.json()
            if not isinstance(result["body"], dict):
                result["body"] = {}
                raise ValueError("Expected a JSON object.")
            response.raise_for_status()
            result["server_age_s"] = (
                time.time() - parsedate_to_datetime(response.headers["Date"]).timestamp()
            )
        except asyncio.CancelledError:
            result["error"] = "CancelledError"
            cancelled = True
        except (
            httpx.HTTPError,
            ValueError,
            KeyError,
            TypeError,
            OverflowError,
            AttributeError,
        ) as exc:
            result["error"] = type(exc).__name__
        result["received"] = time.time()
        result["latency_ms"] = (result["received"] - started) * 1000
        age = result["server_age_s"]
        result["fresh"] = (
            result["error"] is None
            and 0 <= result["latency_ms"] <= 2000
            and age is not None
            and -1 <= age <= 5
        )
        self.db.execute(
            "INSERT INTO requests VALUES(NULL,?,?,?,?,?,?,?,?,?,?)",
            (
                started,
                result["received"],
                provider,
                path,
                result["latency_ms"],
                result["status"],
                age,
                int(result["fresh"]),
                result["error"],
                json.dumps(result["body"]),
            ),
        )
        self.db.commit()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def discover(self, client: httpx.AsyncClient) -> None:
        rejected, active, failure = [], [], None
        try:
            series = await self.get(client, "kalshi-demo", API_PREFIX + "/series/" + SERIES)
            if not series["fresh"]:
                raise ValueError("Series unavailable or stale.")
            self.series = series["body"]["series"]
            verify_series(self.series)
            cursor, seen = "", set()
            for _ in range(10):
                page = await self.get(
                    client,
                    "kalshi-demo",
                    API_PREFIX + "/markets",
                    {
                        "series_ticker": SERIES,
                        "status": "open",
                        "limit": 100,
                        "cursor": cursor,
                    },
                )
                if not page["fresh"]:
                    raise ValueError("Discovery response unavailable or stale.")
                for raw in page["body"]["markets"]:
                    try:
                        market = verify_market(raw, self.series, now=page["received"])
                        active.append(market)
                        self.markets.setdefault(market.ticker, market)
                        self.db.execute(
                            "INSERT OR IGNORE INTO markets VALUES(?,?,?,NULL,NULL,NULL)",
                            (market.ticker, page["received"], json.dumps(asdict(market))),
                        )
                    except (ValueError, KeyError, TypeError) as exc:
                        rejected.append({"ticker": raw.get("ticker"), "reason": str(exc)})
                cursor = page["body"]["cursor"]
                if not cursor:
                    break
                if cursor in seen:
                    raise ValueError("Discovery pagination loop.")
                seen.add(cursor)
            else:
                raise ValueError("Discovery page bound reached.")
        except (ValueError, KeyError, TypeError) as exc:
            failure = str(exc)
            active = []
        self.active = list({m.ticker: m for m in active}.values())
        self.db.execute(
            "INSERT INTO discovery VALUES(?,?,?,?,?)",
            (
                time.time(),
                int(bool(active) and failure is None),
                len(self.active),
                json.dumps(rejected),
                failure,
            ),
        )
        self.db.commit()
        self.next_discovery = time.monotonic() + 30

    async def outcomes(self, client: httpx.AsyncClient) -> None:
        for ticker, market in self.markets.items():
            row = self.db.execute(
                "SELECT outcome FROM markets WHERE ticker=?", (ticker,)
            ).fetchone()
            if market.closes_at > time.time() or row[0] is not None:
                continue
            response = await self.get(
                client, "kalshi-demo", API_PREFIX + "/markets/" + quote(ticker, safe="")
            )
            if not response["fresh"]:
                continue
            try:
                raw = response["body"]["market"]
                if raw.get("status") != "finalized":
                    continue
                current = verify_market(raw, self.series, now=response["received"], finalized=True)
                settled = utc_timestamp(raw["settlement_ts"])
                payout = "1.0000" if raw["result"] == "yes" else "0.0000"
                if (
                    current != market
                    or raw["settlement_value_dollars"] != payout
                    or not market.closes_at <= settled <= response["received"]
                ):
                    raise ValueError("Settlement evidence mismatch.")
                self.db.execute(
                    "UPDATE markets SET outcome=?,confirmed_at=?,final_metadata=? WHERE ticker=?",
                    (int(raw["result"] == "yes"), response["received"], json.dumps(raw), ticker),
                )
            except (ValueError, KeyError, TypeError) as exc:
                self.db.execute(
                    "INSERT INTO run VALUES(?,?) ON CONFLICT(key) DO UPDATE "
                    "SET value=excluded.value",
                    ("outcome_error:" + ticker, json.dumps(str(exc))),
                )
            self.db.commit()
        self.next_settlement = time.monotonic() + 30

    def reference(self, response: dict[str, Any]) -> tuple[ReferenceTick | None, str | None]:
        try:
            raw = response["body"]
            tick = ReferenceTick(timestamp=utc_timestamp(raw["time"]), price=float(raw["price"]))
            bid, ask = float(raw["bid"]), float(raw["ask"])
            if (
                not response["fresh"]
                or not 0 <= time.time() - tick.timestamp <= 5
                or not all(math.isfinite(p) for p in (bid, ask))
                or not 0 < bid <= ask
                or (ask - bid) / tick.price > 0.001
                or abs(tick.price - (bid + ask) / 2) / tick.price > 0.001
            ):
                raise ValueError("Stale/future or invalid Coinbase reference.")
            if self.ticks and tick.timestamp < self.ticks[-1].timestamp:
                raise ValueError("Reference timestamp moved backwards.")
            if self.ticks and tick.timestamp == self.ticks[-1].timestamp:
                if tick.price != self.ticks[-1].price:
                    raise ValueError("Conflicting duplicate reference.")
            else:
                if self.ticks and tick.timestamp - self.ticks[-1].timestamp > 5:
                    self.ticks = []  # restart warm-up, never interpolate a gap
                self.ticks.append(tick)
                self.ticks = self.ticks[-1800:]
            return tick, None
        except (ValueError, KeyError, TypeError) as exc:
            return None, str(exc)

    async def cycle(
        self,
        kalshi: httpx.AsyncClient,
        coinbase: httpx.AsyncClient,
    ) -> None:
        if time.monotonic() >= self.next_discovery or not any(
            m.opens_at <= time.time() < m.closes_at for m in self.active
        ):
            await self.discover(kalshi)
        ref = await self.get(coinbase, "coinbase", CoinbaseReference.URL)
        tick, ref_error = self.reference(ref)
        active = [m for m in self.active if m.opens_at <= time.time() < m.closes_at]
        for market in active:
            metadata = await self.get(
                kalshi, "kalshi-demo", API_PREFIX + "/markets/" + quote(market.ticker, safe="")
            )
            book = await self.get(
                kalshi,
                "kalshi-demo",
                API_PREFIX + "/markets/" + quote(market.ticker, safe="") + "/orderbook",
            )
            now = time.time()
            row: dict[str, Any] = {
                "observed_at": now,
                "ticker": market.ticker,
                "strike": market.strike,
                "opens_at": market.opens_at,
                "closes_at": market.closes_at,
                "reference_price": ref["body"].get("price"),
                "reference_trade_at": tick.timestamp if tick else ref["body"].get("time"),
                "reference_age_s": now - tick.timestamp if tick else None,
                "reference_latency_ms": ref["latency_ms"],
                "book_received_at": book["received"],
                "book_latency_ms": book["latency_ms"],
                "book_http_age_s": book["server_age_s"],
                "metadata_received_at": metadata["received"],
                "metadata_latency_ms": metadata["latency_ms"],
                "metadata_http_age_s": metadata["server_age_s"],
                "reference_http_age_s": ref["server_age_s"],
                "reference_received_at": ref["received"],
                "reference_valid": tick is not None and 0 <= now - tick.timestamp <= 5,
                "diagnostic_only": True,
                "entries_enabled": False,
                "yes_probability": None,
                "yes_net_edge": None,
                "no_net_edge": None,
                "calibrated_net_edge": None,
                "paper_quote_eligible": False,
            }
            errors = []
            missing = any(r["error"] for r in (ref, metadata, book))
            stale = any(not r["fresh"] and not r["error"] for r in (ref, metadata, book))
            if ref_error:
                errors.append(ref_error)
                stale |= bool("Stale" in ref_error)
            model_inputs_valid = False
            try:
                current = verify_market(metadata["body"]["market"], self.series, now=now)
                if current != market:
                    raise ValueError("Metadata changed after discovery.")
                row["market_metadata"] = metadata["body"]["market"]
                if tick is None or not 0 <= now - tick.timestamp <= 5:
                    raise ValueError("Reference unavailable/stale at book receipt.")
                model_inputs_valid = bool(metadata["fresh"] and ref["fresh"])
                row.update(book_fields(book["body"]))
                if row["yes_bid"] is None or row["no_bid"] is None:
                    raise ValueError("One-sided book; complete executable spread unavailable.")
            except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                errors.append(str(exc))
                missing = True
            valid = not errors and not missing and not stale
            if valid:
                try:
                    orderbook_quote(book["body"], market.ticker, book["received"])
                    row["paper_quote_eligible"] = True
                except (ValueError, KeyError, TypeError, ArithmeticError):
                    pass
            if model_inputs_valid:
                try:
                    prediction = estimate(self.ticks, market.strike, market.closes_at, now=now)
                    p = prediction.yes_probability
                    row.update(
                        {
                            "yes_probability": p,
                            "model_id": prediction.model_id,
                            "reference_samples": prediction.reference_samples,
                            "yes_net_edge": p - row["yes_ask"] - 0.03
                            if book["fresh"] and row.get("yes_ask") is not None
                            else None,
                            "no_net_edge": 1 - p - row["no_ask"] - 0.03
                            if book["fresh"] and row.get("no_ask") is not None
                            else None,
                            "edge_cost_assumption": "ask + $0.01 slippage + $0.02 fee/contract",
                            "minimum_haircut_yes_edge": p - 0.05 - row["yes_ask"] - 0.03
                            if book["fresh"] and row.get("yes_ask") is not None
                            else None,
                            "minimum_haircut_no_edge": 1 - p - 0.05 - row["no_ask"] - 0.03
                            if book["fresh"] and row.get("no_ask") is not None
                            else None,
                            "calibration_status": "unvalidated; edges are non-actionable",
                        }
                    )
                except ValueError as exc:
                    row["diagnostic_reason"] = str(exc)
            row["errors"] = errors
            self.record_observation(now, market.ticker, valid, bool(missing), bool(stale), row)
        if not active:
            self.record_observation(
                time.time(),
                None,
                False,
                True,
                False,
                {"errors": ["No verified active market discovered."]},
            )
        self.db.commit()
        if time.monotonic() >= self.next_settlement:
            await self.outcomes(kalshi)

    def record_observation(
        self,
        at: float,
        ticker: str | None,
        valid: bool,
        missing: bool,
        stale: bool,
        payload: dict[str, Any],
    ) -> None:
        self.db.execute(
            "INSERT INTO observations VALUES(NULL,?,?,?,?,?,?)",
            (
                at,
                ticker,
                int(valid),
                int(missing),
                int(stale),
                json.dumps(payload),
            ),
        )

    def finish(
        self,
        started: float,
        elapsed: float,
        requested: float,
        before: dict[str, Any],
        after: dict[str, Any],
        failure: str | None,
    ) -> dict[str, Any]:
        rows = self.db.execute("SELECT valid,missing,stale,payload FROM observations").fetchall()
        payloads = [json.loads(r[3]) for r in rows]
        total = len(rows)
        forecasts = [p for p in payloads if p.get("yes_probability") is not None]
        forecast_markets = {p["ticker"] for p in forecasts}
        labels = dict(self.db.execute("SELECT ticker,outcome FROM markets"))
        resolved = forecast_markets & {t for t, y in labels.items() if y is not None}
        requests = self.db.execute(
            "SELECT provider,status,error,latency_ms,fresh FROM requests"
        ).fetchall()
        discovery = self.db.execute("SELECT success FROM discovery").fetchall()
        report = {
            "status": "completed" if failure is None and elapsed >= requested else "interrupted",
            "started_at_utc": datetime.fromtimestamp(started, UTC).isoformat(),
            "finished_at_utc": datetime.fromtimestamp(time.time(), UTC).isoformat(),
            "requested_seconds": requested,
            "elapsed_seconds": elapsed,
            "failure": failure,
            "environment": "demo",
            "observation_only": True,
            "entries_enabled": False,
            "market_count": len(self.markets),
            "observations": total,
            "valid_observations": sum(r[0] for r in rows),
            "fresh_reference_observations": sum(bool(p.get("reference_valid")) for p in payloads),
            "missing_or_stale_percent": (
                100 * sum(not r[0] for r in rows) / total if total else None
            ),
            "missing_percent": 100 * sum(r[1] for r in rows) / total if total else None,
            "stale_percent": 100 * sum(r[2] for r in rows) / total if total else None,
            "quality_denominator": "market sampling opportunities, including discovery failures",
            "discovery_attempts": len(discovery),
            "discovery_successes": sum(r[0] for r in discovery),
            "discovery_success_percent": (
                100 * sum(r[0] for r in discovery) / len(discovery) if discovery else None
            ),
            "diagnostic_forecasts": len(forecasts),
            "forecast_market_count": len(forecast_markets),
            "resolved_forecast_markets": len(resolved),
            "finalized_markets": sum(y is not None for y in labels.values()),
            "pending_outcomes": [t for t, y in labels.items() if y is None],
            "calibration_ready": False,
            "calibration_reason": (
                "No audited out-of-time artifact; gate needs >=200 distinct markets "
                "plus matching horizon/bin coverage and score thresholds."
            ),
            "api_requests": len(requests),
            "api_errors": sum(r[2] is not None for r in requests),
            "api_error_breakdown": dict(
                Counter(f"{p}:{s}:{e}" for p, s, e, _, _ in requests if e is not None)
            ),
            "http_status_counts": dict(Counter(f"{p}:{s}" for p, s, _, _, _ in requests)),
            "http_stale_or_slow_responses": sum(not r[4] and r[2] is None for r in requests),
            "maximum_request_latency_ms": max((r[3] for r in requests), default=None),
            "paper_ledger_before": before,
            "paper_ledger_after": after,
            "paper_ledger_unchanged": before == after,
            "promotion_allowed": False,
            "settlement_policy": "only finalized verified demo outcomes seen before stop; "
            "no outcome inferred from Coinbase",
        }
        self.db.execute("INSERT INTO run VALUES('summary',?)", (json.dumps(report),))
        self.db.commit()
        self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.db.close()
        self.output.with_suffix(".json").write_text(json.dumps(report, indent=2) + "\n")
        fields = sorted({k for p in payloads for k in p})
        with self.output.with_suffix(".csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for p in payloads:
                writer.writerow(
                    {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in p.items()}
                )
        return report


async def run_capture(
    seconds: float,
    interval: float,
    output: Path,
    ledger: Path,
) -> dict[str, Any]:
    require_collection_release()
    require_simulation()
    if not 0 < seconds <= 1800 or not 1 <= interval <= 5:
        raise ValueError("Only bounded <=30-minute observations at 1-5 second intervals.")
    if output.resolve() == ledger.resolve():
        raise ValueError("Observation database must not be the paper ledger.")
    before = ledger_state(ledger)
    if before["account_cents"] != (5000, 5000, 0, 0) or before["orders"] or before["fills"]:
        raise ValueError("Expected untouched $50 paper account; refusing to alter it.")
    observer = Observer(output)
    started, monotonic = time.time(), time.monotonic()
    failure = None
    try:
        async with (
            KalshiClient("https://demo-api.kalshi.co", timeout=2) as kalshi,
            httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False) as coinbase,
        ):
            while (remaining := seconds - (time.monotonic() - monotonic)) > 0:
                cycle_started = time.monotonic()
                try:
                    await asyncio.wait_for(observer.cycle(kalshi._client, coinbase), remaining)
                except TimeoutError:
                    break
                await asyncio.sleep(
                    min(
                        max(0, interval - (time.monotonic() - cycle_started)),
                        max(0, seconds - (time.monotonic() - monotonic)),
                    )
                )
    except (Exception, asyncio.CancelledError) as exc:
        failure = type(exc).__name__ + ": " + str(exc)
    report = observer.finish(
        started, time.monotonic() - monotonic, seconds, before, ledger_state(ledger), failure
    )
    print(json.dumps(report, indent=2), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=Path("data/paper.db"))
    args = parser.parse_args()
    result = asyncio.run(run_capture(args.seconds, args.interval, args.output, args.ledger))
    raise SystemExit(0 if result["status"] == "completed" else 2)
