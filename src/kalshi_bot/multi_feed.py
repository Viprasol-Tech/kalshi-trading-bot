"""Bounded unsigned demo discovery and generic paper-loop plumbing.

No production URLs, exchange account reads, order writes or credential loading.
Quote timestamps are never taken from HTTP Date or market last_updated_time.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from urllib.parse import quote

from kalshi_bot.btc_collect import quote_timestamp
from kalshi_bot.btc_observe import book_fields
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.multi_market import CATEGORY_NAMES, Book, Category, Contract, PaperEngine, canonical
from kalshi_bot.safety import SimulationOnlyError, require_simulation


class MultiFeed:
    def __init__(self, engine: PaperEngine) -> None:
        require_simulation()
        self.engine = engine
        self.db = engine.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS mm_reads(
              id INTEGER PRIMARY KEY,at REAL,path TEXT,parameters TEXT,body TEXT,error TEXT);
            CREATE TABLE IF NOT EXISTS mm_candidates(
              ticker TEXT PRIMARY KEY,raw TEXT,checked REAL,reason TEXT,category TEXT);
            CREATE TABLE IF NOT EXISTS mm_feed_state(
              id INTEGER PRIMARY KEY,cursor TEXT,cycles INTEGER,failures INTEGER);
            INSERT OR IGNORE INTO mm_feed_state VALUES(1,'',0,0);
            CREATE TABLE IF NOT EXISTS mm_series_seen(prefix TEXT PRIMARY KEY,at REAL);
        """)
        self.reads = 0

    async def get(
        self, client: KalshiClient, path: str, params: dict[str, Any] | None = None
    ) -> tuple[dict[str, Any], float]:
        if (
            self.reads >= 30
            or self.db.execute("SELECT failures FROM mm_feed_state").fetchone()[0] >= 10
        ):
            raise SimulationOnlyError("Read budget/error circuit breaker reached.")
        self.reads += 1
        started = time.monotonic()
        try:
            body = await client._request("GET", path, params=params or {})
            at = time.time()
            if time.monotonic() - started > 2:
                raise ValueError("Market data transport too slow.")
        except Exception as exc:
            self.db.execute("UPDATE mm_feed_state SET failures=failures+1")
            self.db.execute(
                "INSERT INTO mm_reads VALUES(NULL,?,?,?,?,?)",
                (time.time(), path, canonical(params or {}), None, type(exc).__name__),
            )
            raise
        self.db.execute("UPDATE mm_feed_state SET failures=0")
        self.db.execute(
            "INSERT INTO mm_reads VALUES(NULL,?,?,?,?,NULL)",
            (at, path, canonical(params or {}), canonical(body)),
        )
        return body, at

    async def scan(self, client: KalshiClient) -> list[tuple[Contract, Book]]:
        """One page/cycle with periodic first-page reseeding for rollover.

        Candidates hydrate round-robin; no category/strike inference from titles.
        Series prefix is only an API lookup hint, verified against returned IDs.
        Actual category comes from series metadata. Unsupported rules stay blocked.
        """
        require_simulation()
        self.reads = 0
        state = self.db.execute("SELECT * FROM mm_feed_state").fetchone()
        cursor = state["cursor"]
        # Reseed the newest page WITHOUT resetting the full-universe cursor.
        # Otherwise a five-cycle reseed would permanently hide page six onward.
        pages = []
        if cursor and state["cycles"] % 5 == 0:
            seed, _ = await self.get(
                client, "/markets", {"status": "open", "limit": 100, "cursor": ""}
            )
            pages.append(seed)
        page, _ = await self.get(
            client, "/markets", {"status": "open", "limit": 100, "cursor": cursor}
        )
        pages.append(page)
        for catalog in pages:
            for raw in catalog.get("markets", []):
                if not isinstance(raw, dict) or not isinstance(raw.get("ticker"), str):
                    continue
                self.db.execute(
                    "INSERT INTO mm_candidates VALUES(?,?,0,'unverified',NULL) "
                    "ON CONFLICT(ticker) DO UPDATE SET raw=excluded.raw",
                    (raw["ticker"], canonical(raw)),
                )
        self.db.execute(
            "UPDATE mm_feed_state SET cursor=?,cycles=cycles+1", (page.get("cursor", ""),)
        )
        frames = []
        rows = self.db.execute("""
            WITH base AS (
              SELECT *,substr(json_extract(raw,'$.event_ticker'),1,
                instr(json_extract(raw,'$.event_ticker'),'-')-1) AS lookup_prefix
              FROM mm_candidates
            ), ranked AS (
              SELECT *,ROW_NUMBER() OVER(
                PARTITION BY lookup_prefix ORDER BY checked,ticker) AS rn FROM base
            )
            SELECT r.* FROM ranked r LEFT JOIN mm_series_seen s ON s.prefix=r.lookup_prefix
            ORDER BY r.rn,COALESCE(s.at,0),r.checked,r.ticker LIMIT 8
        """).fetchall()
        for row in rows:
            category = None
            prefix = ""
            try:
                listed = json.loads(row["raw"])
                event = listed["event_ticker"]
                prefix = event.split("-")[0]
                series, _ = await self.get(client, "/series/" + quote(prefix, safe=""))
                raw, at = await self.get(client, "/markets/" + quote(row["ticker"], safe=""))
                if (
                    series["series"].get("ticker") != prefix
                    or raw["market"].get("ticker") != row["ticker"]
                    or raw["market"].get("event_ticker") != event
                ):
                    raise ValueError("Discovery/detail response identity mismatch.")
                reported = CATEGORY_NAMES.get(
                    str(series["series"].get("category", "")).strip().lower()
                )
                category = reported.value if reported is not None else None
                contract = Contract.verify(raw["market"], series["series"], at=at, now=time.time())
                category = contract.category.value
                body, received = await self.get(
                    client,
                    "/markets/" + quote(contract.ticker, safe="") + "/orderbook",
                    {"depth": 10},
                )
                stamp = quote_timestamp(body, received)
                if stamp["quote_timestamp_status"] != "fresh":
                    raise ValueError("Actual order-book event timestamp unavailable/stale.")
                fields = book_fields(body)
                book = Book(
                    contract.ticker,
                    "demo:" + str(self.db.execute("SELECT MAX(id) FROM mm_reads").fetchone()[0]),
                    stamp["quote_timestamp"],
                    received,
                    fields["yes_bid"],
                    fields["no_bid"],
                    fields["yes_bid_depth"],
                    fields["no_bid_depth"],
                )
                book.check(now=time.time())
                frames.append((contract, book))
                reason = "quote verified; model release still required"
            except (ValueError, KeyError, TypeError) as exc:
                reason = str(exc)
            self.db.execute(
                "UPDATE mm_candidates SET checked=?,reason=?,category=? WHERE ticker=?",
                (time.time(), reason, category, row["ticker"]),
            )
            self.db.execute(
                "INSERT INTO mm_series_seen VALUES(?,?) "
                "ON CONFLICT(prefix) DO UPDATE SET at=excluded.at",
                (prefix, time.time()),
            )
        return frames

    async def outcomes(self, client: KalshiClient, *, now: float) -> None:
        """Official settled labels only, bounded and round-robin across held markets."""
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS mm_label_checks(ticker TEXT PRIMARY KEY,at REAL)"
        )
        rows = self.db.execute(
            "SELECT i.* FROM mm_intents i LEFT JOIN mm_label_checks l ON l.ticker=i.ticker "
            "WHERE i.status IN ('held','pending') AND json_extract(i.contract,'$.closes')<=? "
            "ORDER BY COALESCE(l.at,0),i.ticker LIMIT 3",
            (now,),
        ).fetchall()
        for row in rows:
            data = json.loads(row["contract"])
            data["category"] = Category(data["category"])
            contract = Contract(**data)
            body, received = await self.get(client, "/markets/" + quote(contract.ticker, safe=""))
            self.db.execute(
                "INSERT INTO mm_label_checks VALUES(?,?) "
                "ON CONFLICT(ticker) DO UPDATE SET at=excluded.at",
                (contract.ticker, received),
            )
            if body["market"].get("status") == "settled":
                self.engine.settle(contract, body["market"], received_at=received, now=time.time())


async def run(engine: PaperEngine, *, seconds: float, interval: float = 5) -> None:
    """Bounded future paper runner. Never activates/unarms a killed trial.

    Fixed UTC deadline is persisted before IO; restarting never extends it.
    Both UTC and monotonic clocks limit a segment. No service is installed/started.
    """
    require_simulation()
    if not 1 <= seconds <= 21600 or not 2 <= interval <= 5:
        raise ValueError("Bounded <=6-hour run at 2-5-second intervals required.")
    if engine.db.execute("SELECT killed FROM mm_control").fetchone()[0] or not engine.models:
        raise SimulationOnlyError(
            "No activated, validated model trial. Runner will not access a feed."
        )
    from kalshi_bot.multi_market import require_model_release

    for category, model in engine.models.items():
        require_model_release(category, model.model_id)
    engine.db.execute("CREATE TABLE IF NOT EXISTS mm_session(id INTEGER PRIMARY KEY,deadline REAL)")
    engine.db.execute("INSERT OR IGNORE INTO mm_session VALUES(1,?)", (time.time() + seconds,))
    deadline = engine.db.execute("SELECT deadline FROM mm_session").fetchone()[0]
    remaining = min(seconds, deadline - time.time())
    if remaining <= 0:
        engine.shutdown(now=time.time(), reason="original session deadline reached")
        return
    mono_end = time.monotonic() + remaining
    feed = MultiFeed(engine)
    stop_reason = "bounded runner stopped; inventory retained"
    try:
        async with KalshiClient("https://demo-api.kalshi.co", timeout=2) as client:
            while time.time() < deadline and time.monotonic() < mono_end:
                if engine.db.execute("SELECT killed FROM mm_control").fetchone()[0]:
                    break
                left = min(deadline - time.time(), mono_end - time.monotonic())
                frames = await asyncio.wait_for(feed.scan(client), timeout=left)
                now = time.time()
                engine.cycle(frames, now=now)
                await asyncio.wait_for(
                    feed.outcomes(client, now=now),
                    timeout=max(0.001, min(deadline - time.time(), mono_end - time.monotonic())),
                )
                attempts = engine.db.execute(
                    "SELECT COUNT(*) FROM mm_candidates WHERE checked>0 AND category IS NOT NULL"
                ).fetchone()[0]
                valid = engine.db.execute(
                    "SELECT COUNT(*) FROM mm_candidates "
                    "WHERE reason='quote verified; model release still required'"
                ).fetchone()[0]
                if attempts >= 20 and valid / attempts < 0.8:
                    raise ValueError("Quote coverage below 80%; stop rather than weaken freshness.")
                last = engine.db.execute("SELECT MAX(at) FROM mm_quotes").fetchone()[0]
                first = engine.db.execute("SELECT MIN(at) FROM mm_reads").fetchone()[0]
                if now - (last if last is not None else first) >= 600:
                    raise ValueError("No verified target-market quote for ten minutes.")
                await asyncio.sleep(
                    max(0, min(interval, deadline - time.time(), mono_end - time.monotonic()))
                )
    except BaseException as exc:
        stop_reason = "runner failure: " + type(exc).__name__ + "; inventory retained"
        raise
    finally:
        # Never overwrite an operator emergency stop or an integrity-failure
        # reason already persisted by the controller.
        if not engine.db.execute("SELECT killed FROM mm_control").fetchone()[0]:
            engine.shutdown(now=time.time(), reason=stop_reason)
