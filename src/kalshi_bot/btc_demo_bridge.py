"""Demo-only collector/supervisor integration. ALL collection entrypoints sealed.

No network use on import/construction. Reuses Collector discovery, raw-book,
proxy-reference and finalized-label verification in the SAME campaign SQLite DB.
Only tests patch the collection gate with socket access separately forbidden.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

import httpx

from kalshi_bot.btc_collect import QUALITY_POLICY, Collector, quote_timestamp
from kalshi_bot.btc_data import CoinbaseReference
from kalshi_bot.btc_model import ReferenceTick
from kalshi_bot.btc_observe import Observer, book_fields
from kalshi_bot.btc_rules import VerifiedMarket
from kalshi_bot.btc_supervisor import (
    TERMINAL,
    Frame,
    HostHealth,
    Supervisor,
    SupervisorStopped,
    canonical,
)
from kalshi_bot.exchange.client import KalshiClient, validate_public_request
from kalshi_bot.safety import SimulationOnlyError, require_collection_release, require_simulation


def encode(frame: Frame) -> str:
    return canonical(
        {
            "at": frame.at,
            "payload": frame.payload,
            "series": frame.series,
            "ticks": [t.model_dump(mode="json") for t in frame.ticks],
            "requests": frame.requests,
            "discoveries": frame.discoveries,
        }
    )


def decode(value: str) -> Frame:
    raw = json.loads(value)
    return Frame(
        raw["at"],
        raw["payload"],
        raw["series"],
        tuple(ReferenceTick.model_validate(t) for t in raw["ticks"]),
        tuple(tuple(r) for r in raw["requests"]),
        tuple(raw["discoveries"]),
    )


async def proxy_guard(request: httpx.Request) -> None:
    require_simulation()
    if (
        request.method != "GET"
        or str(request.url) != CoinbaseReference.URL
        or any(
            k.lower().startswith(("authorization", "kalshi-access", "cookie"))
            for k in request.headers
        )
    ):
        raise SimulationOnlyError("Only unsigned Coinbase BTC proxy GET is allowed.")


class CampaignCollector(Collector):
    """Internal single-cycle producer, NOT the standalone six-hour runner.

    Its connection belongs to Supervisor. Global deadlines/locks/quality checks
    belong to Supervisor, not to a fresh legacy six-hour manifest. No own close,
    summary or independent collection loop may be used.
    """

    def __init__(self, bridge: DemoBridge) -> None:
        require_simulation()
        self.bridge = bridge
        supervisor = bridge.supervisor
        self.output, self.db, self.ledger = supervisor.path, supervisor.db, supervisor.ledger
        Observer.initialize_tables(self.db)
        Collector.initialize_collector_tables(self.db)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS request_parameters "
            "(request_id INTEGER PRIMARY KEY REFERENCES requests(id),parameters TEXT NOT NULL)"
        )
        self.initialize_state()
        self.interval, self.slot, self.failures = 2.0, -1, 0
        self.request_errors = {}
        self.current_quotes = {}
        self.candidates = {}
        self.next_listing = 0.0
        self.stop_reason = None
        self.manifest = {
            "started": supervisor.protocol.starts_at,
            "deadline": supervisor.protocol.deadline,
            "reference_source": supervisor.protocol.reference_source,
        }
        state = supervisor.control()
        self.last_verified, self.last_healthy = state["last_verified"], state["last_healthy"]
        # Warm-up always resets after interruption. Never interpolate missing ticks.
        for row in self.db.execute("SELECT ticker,metadata FROM supervisor_markets"):
            market = VerifiedMarket(**json.loads(row["metadata"]))
            self.markets[market.ticker] = market
            self.db.execute(
                "INSERT OR IGNORE INTO markets VALUES(?,?,?,NULL,NULL,NULL)",
                (market.ticker, time.time(), row["metadata"]),
            )

    async def get(
        self,
        client: httpx.AsyncClient,
        provider: str,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        require_collection_release()
        self.bridge.check()
        if path.endswith("/orderbook") and params is None:
            params = {"depth": 10}  # bounded real top-of-book; never claim full-book depth
        # The final HTTP hooks guard actual destination/method/headers. Also
        # reject a mismatched client here before issuing ANY request.
        if provider == "kalshi-demo":
            if str(client.base_url).rstrip("/") != "https://demo-api.kalshi.co":
                raise SimulationOnlyError("Unexpected demo collector client.")
            validate_public_request(client.build_request("GET", path, params=params))
        elif provider != "coinbase" or path != CoinbaseReference.URL:
            raise SimulationOnlyError("Unexpected data provider/path.")
        else:
            await proxy_guard(client.build_request("GET", path, params=params))
        if client.follow_redirects:
            raise SimulationOnlyError("Collector redirects are forbidden.")
        result = await Observer.get(self, client, provider, path, params)
        self.db.execute(
            "INSERT INTO request_parameters VALUES((SELECT MAX(id) FROM requests),?)",
            (canonical(params or {}),),
        )
        self.bridge.sync_receipts()
        if path.endswith("/orderbook"):
            self.current_quotes[path.split("/")[-2]] = quote_timestamp(
                result["body"], result["received"]
            )
        return result

    async def outcomes(self, client: httpx.AsyncClient) -> None:
        # Bounded round-robin: a delayed/unfinalized oldest market cannot starve
        # every newer settlement, nor trigger hundreds of GETs in one cycle.
        cursor = self.db.execute(
            "SELECT value FROM bridge_state WHERE key='settlement_cursor'"
        ).fetchone()
        key = cursor[0] if cursor else ""
        rows = self.db.execute(
            "SELECT ticker FROM markets WHERE outcome IS NULL "
            "AND json_extract(metadata,'$.closes_at')<=? "
            "ORDER BY (ticker<=?),ticker LIMIT 4",
            (time.time(), key),
        ).fetchall()
        all_markets = self.markets
        try:
            self.markets = {r[0]: all_markets[r[0]] for r in rows}
            await Observer.outcomes(self, client)
        finally:
            self.markets = all_markets
        if rows:
            self.db.execute(
                "INSERT INTO bridge_state VALUES('settlement_cursor',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (rows[-1][0],),
            )

    def close(self) -> None:
        raise RuntimeError("Shared collector cannot close the supervisor database.")


class DemoBridge:
    def __init__(self, supervisor: Supervisor) -> None:
        require_simulation()
        supervisor._verify_fingerprints()
        if supervisor.control()["status"] in TERMINAL:
            raise SupervisorStopped("Cannot attach to a stopped campaign.")
        self.supervisor = supervisor
        self.health: HostHealth | None = None
        self.db = supervisor.db
        if self.db.in_transaction:
            raise ValueError("Cannot attach during an unfinished transaction.")
        raw_exists = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='requests'"
        ).fetchone()
        owned = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bridge_manifest'"
        ).fetchone()
        if raw_exists and not owned:
            raise ValueError("Never adopt unrelated raw observations.")
        # Legacy methods commit their own individual raw receipts. Explicit
        # Supervisor transactions still atomically commit counters/forecasts.
        self.db.isolation_level = None
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS bridge_manifest(
                id INTEGER PRIMARY KEY CHECK(id=1),canonical TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bridge_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bridge_requests(request_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS bridge_discoveries(discovery_id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS bridge_batches(
                id INTEGER PRIMARY KEY, started REAL NOT NULL, state TEXT NOT NULL,
                frame TEXT, reason TEXT,frame_sha256 TEXT
            );
            CREATE TABLE IF NOT EXISTS bridge_outcomes(
                ticker TEXT PRIMARY KEY, received REAL NOT NULL, raw TEXT NOT NULL,
                state TEXT NOT NULL
            );
        """)
        manifest = canonical(
            {"version": 1, "protocol": json.loads(supervisor.protocol.canonical())}
        )
        row = self.db.execute("SELECT canonical FROM bridge_manifest").fetchone()
        if row is not None and row[0] != manifest:
            raise ValueError("Frozen bridge identity changed.")
        self.db.execute("INSERT OR IGNORE INTO bridge_manifest VALUES(1,?)", (manifest,))
        self.collector = CampaignCollector(self)

    def check(self) -> None:
        state = self.supervisor.advance(
            now=time.time(), monotonic=time.monotonic(), health=self.health
        )
        if state["status"] != "running":
            raise SupervisorStopped("Campaign not running; no additional HTTP reads.")

    def sync_receipts(self) -> None:
        """Exactly-once provider/discovery counters BEFORE another GET is allowed.

        Requests can commit before a process crash. Resume replays their IDs,
        not the network, before issuing new reads. Counters never double-count
        when the completed Frame is later retried.
        """
        with self.supervisor.transaction():
            counters = json.loads(self.supervisor.control()["provider_errors"])
            rows = self.db.execute(
                "SELECT r.* FROM requests r LEFT JOIN bridge_requests b "
                "ON r.id=b.request_id WHERE b.request_id IS NULL ORDER BY r.id"
            ).fetchall()
            for row in rows:
                provider = row["provider"]
                if provider not in ("kalshi-demo", "coinbase"):
                    self.supervisor._stop("safety_stop", "unknown_raw_provider", time.time())
                    break
                success = bool(row["fresh"] and row["error"] is None)
                counters[provider] = 0 if success else counters.get(provider, 0) + 1
                self.db.execute("INSERT INTO bridge_requests VALUES(?)", (row["id"],))
                self.db.execute(
                    "UPDATE supervisor_totals SET api_errors=api_errors+? WHERE id=1",
                    (int(row["error"] is not None),),
                )
                if counters[provider] >= QUALITY_POLICY["max_consecutive_request_errors"]:
                    self.supervisor._stop(
                        "quality_stop", "consecutive_provider_errors", time.time()
                    )
            self.db.execute(
                "UPDATE supervisor_control SET provider_errors=? WHERE id=1",
                (canonical(counters),),
            )
            for row in self.db.execute(
                "SELECT d.rowid AS receipt,d.* FROM discovery d "
                "LEFT JOIN bridge_discoveries b ON d.rowid=b.discovery_id "
                "WHERE b.discovery_id IS NULL ORDER BY d.rowid"
            ).fetchall():
                self.db.execute("INSERT INTO bridge_discoveries VALUES(?)", (row["receipt"],))
                self.db.execute(
                    "UPDATE supervisor_totals SET discovery_attempts=discovery_attempts+1,"
                    "discovery_successes=discovery_successes+? WHERE id=1",
                    (int(bool(row["success"])),),
                )
                if any(
                    "identity changed" in str(r.get("reason", "")).lower()
                    for r in json.loads(row["rejected"])
                ):
                    self.supervisor._stop(
                        "safety_stop", "verified_market_identity_changed", time.time()
                    )
        if self.supervisor.control()["status"] in TERMINAL:
            raise SupervisorStopped("Persistent campaign stop; no further requests.")

    def recover(self) -> None:
        self.sync_receipts()
        self.db.execute(
            "UPDATE bridge_batches SET state='interrupted',reason='incomplete source batch' "
            "WHERE state='polling'"
        )
        for row in self.db.execute(
            "SELECT id,frame FROM bridge_batches WHERE state='prepared' ORDER BY id"
        ).fetchall():
            self.accept(row["id"], row["frame"])
        # Old labels are audited, not passed off as newly received after restart.
        # Their original HTTP body/receipt remains immutable in requests.
        for row in self.db.execute(
            "SELECT ticker,confirmed_at,final_metadata FROM markets "
            "WHERE outcome IS NOT NULL AND ticker NOT IN (SELECT ticker FROM bridge_outcomes)"
        ).fetchall():
            if time.time() - row["confirmed_at"] > 5:
                self.db.execute(
                    "UPDATE markets SET outcome=NULL,confirmed_at=NULL,final_metadata=NULL "
                    "WHERE ticker=?",
                    (row["ticker"],),
                )
                with self.supervisor.transaction():
                    self.supervisor.event(
                        "settlement_refresh_required", time.time(), {"ticker": row["ticker"]}
                    )

    def accept(self, batch: int, encoded: str) -> None:
        fingerprint = hashlib.sha256(encoded.encode()).hexdigest()
        stored = self.db.execute(
            "SELECT frame_sha256 FROM bridge_batches WHERE id=?", (batch,)
        ).fetchone()
        if stored is None or stored[0] != fingerprint:
            with self.supervisor.transaction():
                self.supervisor._stop("safety_stop", "corrupted_bridge_batch", time.time())
            raise SupervisorStopped("Source batch fingerprint mismatch.")
        frame = decode(encoded)
        slot = int((frame.at - self.supervisor.protocol.starts_at) // 2)
        old = self.db.execute(
            "SELECT sha256 FROM supervisor_frames WHERE slot=?", (slot,)
        ).fetchone()
        if old is not None:
            if old[0] != fingerprint:
                with self.supervisor.transaction():
                    self.supervisor._stop("safety_stop", "conflicting_bridge_retry", time.time())
                raise SupervisorStopped("Conflicting bridge receipt.")
            state = "accepted"
        elif (
            slot != int((time.time() - self.supervisor.protocol.starts_at) // 2)
            or not 0 <= time.time() - frame.at <= 2
        ):
            state = "expired_unrecorded"  # never backfill an outage slot
        else:
            self.supervisor.ingest(
                frame, now=time.time(), monotonic=time.monotonic(), health=self.health
            )
            state = "accepted"
        # Accepted evidence is already durably stored in supervisor_frames.
        # Avoid retaining a second large copy of every frame/tick history.
        self.db.execute(
            "UPDATE bridge_batches SET state=?,frame=CASE WHEN ?='accepted' THEN NULL "
            "ELSE frame END WHERE id=?",
            (state, state, batch),
        )

    def forward_outcomes(self) -> None:
        for row in self.db.execute(
            "SELECT ticker,confirmed_at,final_metadata FROM markets WHERE outcome IS NOT NULL "
            "AND ticker NOT IN (SELECT ticker FROM bridge_outcomes) ORDER BY confirmed_at LIMIT 4"
        ).fetchall():
            known = self.db.execute(
                "SELECT 1 FROM supervisor_markets WHERE ticker=?", (row["ticker"],)
            ).fetchone()
            state = "rejected_unobserved"
            if known:
                self.supervisor.settlement(
                    json.loads(row["final_metadata"]),
                    self.collector.series,
                    received_at=row["confirmed_at"],
                    now=time.time(),
                    monotonic=time.monotonic(),
                    health=self.health,
                )
                state = "accepted"
            self.db.execute(
                "INSERT INTO bridge_outcomes VALUES(?,?,?,?)",
                (row["ticker"], row["confirmed_at"], row["final_metadata"], state),
            )

    async def poll_once(self, health: HostHealth) -> dict[str, Any]:
        require_collection_release()  # BEFORE recovery, state writes or client construction
        self.health = health
        self.recover()  # raw crash receipts must stop the next GET if necessary
        self.check()
        current = int((time.time() - self.supervisor.protocol.starts_at) // 2)
        if self.db.execute("SELECT 1 FROM supervisor_frames WHERE slot=?", (current,)).fetchone():
            return self.supervisor.report()
        start_request = self.db.execute("SELECT COALESCE(MAX(id),0) FROM requests").fetchone()[0]
        start_observation = self.db.execute(
            "SELECT COALESCE(MAX(id),0) FROM observations"
        ).fetchone()[0]
        batch = self.db.execute(
            "INSERT INTO bridge_batches(started,state) VALUES(?,'polling')", (time.time(),)
        ).lastrowid
        assert batch is not None
        self.collector.slot = current
        self.collector.current_quotes.clear()
        reason = None
        try:
            async with (
                KalshiClient("https://demo-api.kalshi.co", timeout=2) as kalshi,
                httpx.AsyncClient(
                    timeout=2,
                    follow_redirects=False,
                    trust_env=False,
                    event_hooks={"request": [proxy_guard]},
                ) as proxy,
            ):
                await asyncio.wait_for(
                    self.collector.cycle(kalshi._client, proxy),
                    timeout=min(2, self.supervisor.protocol.deadline - time.time()),
                )
        except TimeoutError:
            reason = "cycle_timeout"
        except SupervisorStopped:
            self.db.execute(
                "UPDATE bridge_batches SET state='stopped',reason='persistent stop' WHERE id=?",
                (batch,),
            )
            raise
        except Exception as exc:
            with self.supervisor.transaction():
                self.supervisor._stop(
                    "safety_stop", "collector_exception:" + type(exc).__name__, time.time()
                )
            raise
        finally:
            self.sync_receipts()
        self.check()
        observations = self.db.execute(
            "SELECT payload FROM observations WHERE id>? ORDER BY id", (start_observation,)
        ).fetchall()
        if len(observations) > 1:
            with self.supervisor.transaction():
                self.supervisor._stop(
                    "safety_stop", "multiple_active_market_observations", time.time()
                )
            raise SupervisorStopped("Ambiguous active market set; no quote chosen.")
        payload = (
            json.loads(observations[0][0])
            if observations and reason is None
            else {
                "errors": [reason or "no_observation"],
                "entries_enabled": False,
            }
        )
        payload["collector_request_receipt_ids"] = [
            r[0]
            for r in self.db.execute(
                "SELECT id FROM requests WHERE id>? ORDER BY id", (start_request,)
            )
        ]
        payload["collector_batch_id"] = batch
        payload["capture_completed_at"] = time.time()
        payload["orderbook_depth_limit"] = 10
        if payload.get("raw_book") is not None:
            raw_quotes = book_fields({"orderbook_fp": payload["raw_book"]})
            for side in ("yes", "no"):
                bid, ask = raw_quotes[side + "_bid"], raw_quotes[side + "_ask"]
                payload[side + "_spread_dollars"] = (
                    ask - bid if (ask is not None and bid is not None) else None
                )
        at = time.time()
        ticker = payload.get("ticker")
        market = self.collector.markets.get(ticker) if isinstance(ticker, str) else None
        # Only the prespecified 300±30s forecast window needs a frozen copy of
        # model history. All individual reference receipts remain in SQLite.
        ticks = (
            tuple(self.collector.ticks)
            if (market is not None and 270 <= market.closes_at - at <= 330)
            else ()
        )
        frame = Frame(at, payload, self.collector.series, ticks)
        encoded = encode(frame)
        self.db.execute(
            "UPDATE bridge_batches SET state='prepared',frame=?,reason=?,frame_sha256=? WHERE id=?",
            (encoded, reason, hashlib.sha256(encoded.encode()).hexdigest(), batch),
        )
        self.accept(batch, encoded)
        self.forward_outcomes()
        return self.supervisor.report()

    async def run(
        self, health_probe: Callable[[], HostHealth], stop: asyncio.Event
    ) -> dict[str, Any]:
        require_collection_release()  # No runnable CLI/service or environment escape hatch.
        while self.supervisor.control()["status"] not in TERMINAL:
            if stop.is_set():
                self.supervisor.stop(now=time.time())
                break
            try:
                await self.poll_once(health_probe())
            except SupervisorStopped:
                if self.supervisor.control()["status"] not in TERMINAL:
                    raise
                break
            now = time.time()
            start = self.supervisor.protocol.starts_at
            next_slot = start + (int((now - start) // 2) + 1) * 2
            delay = max(0, min(next_slot, self.supervisor.protocol.deadline) - now)
            if delay:
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=delay)
        return self.supervisor.report()
