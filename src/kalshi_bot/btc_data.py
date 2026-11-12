"""Unsigned, bounded demo Kalshi reads and a public Coinbase reference proxy."""

from __future__ import annotations

import hashlib
import json
import math
import time
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote

import httpx

from kalshi_bot.btc_model import ReferenceTick
from kalshi_bot.btc_rules import SERIES, VerifiedMarket, utc_timestamp, verify_market, verify_series
from kalshi_bot.exchange.client import API_PREFIX, KalshiClient
from kalshi_bot.paper import PaperQuote
from kalshi_bot.safety import require_simulation


def validate_response(response: httpx.Response, started: float) -> float:
    response.raise_for_status()
    received = time.time()
    server_time = parsedate_to_datetime(response.headers["Date"]).timestamp()
    if not 0 <= received - started <= 2 or not -1 <= received - server_time <= 5:
        raise ValueError("Slow response or stale/future HTTP Date; skip.")
    return received


class DemoBTCSource:
    def __init__(self, client: KalshiClient) -> None:
        self.client = client
        self.last_metadata: dict[str, Any] = {}
        self.last_result: dict[str, Any] = {}

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], float]:
        require_simulation()
        suffix = path if self.client._base_url.endswith(API_PREFIX) else API_PREFIX + path
        started = time.time()
        response = await self.client._client.get(
            suffix, params=params, headers={"Cache-Control": "no-cache"}
        )
        received = validate_response(response, started)
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Unexpected API response.")
        return body, received

    async def discover(self) -> tuple[list[VerifiedMarket], list[dict[str, str]]]:
        data, _ = await self.get("/series/" + SERIES)
        series = data["series"]
        verify_series(series)
        markets, skipped = [], []
        cursor = ""
        seen_cursors: set[str] = set()
        seen_markets: set[str] = set()
        for _ in range(10):
            params = {"series_ticker": SERIES, "status": "open", "limit": 100, "cursor": cursor}
            page, received = await self.get("/markets", params)
            for raw in page["markets"]:
                try:
                    market = verify_market(raw, series, now=received)
                    if market.ticker not in seen_markets:
                        markets.append(market)
                        seen_markets.add(market.ticker)
                except (ValueError, KeyError, TypeError) as exc:
                    skipped.append(
                        {"ticker": str(raw.get("ticker", "unknown")), "reason": str(exc)}
                    )
            cursor = page["cursor"]
            if not cursor:
                return sorted(markets, key=lambda m: m.closes_at), skipped
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise ValueError("Repeated/invalid market pagination cursor.")
            seen_cursors.add(cursor)
        raise ValueError("Discovery exceeded page bound; refusing a partial universe.")

    async def refresh(
        self,
        market: VerifiedMarket,
    ) -> tuple[VerifiedMarket, PaperQuote]:
        data, series_received = await self.get("/series/" + SERIES)
        details, received = await self.get("/markets/" + quote(market.ticker, safe=""))
        current = verify_market(details["market"], data["series"], now=received)
        if current != market:
            raise ValueError("Market metadata changed after discovery.")
        self.last_metadata = {
            "series": data["series"],
            "market": details["market"],
            "metadata_received_at": received,
            "series_received_at": series_received,
        }
        book, observed = await self.get("/markets/" + quote(market.ticker, safe="") + "/orderbook")
        return current, orderbook_quote(book, current.ticker, observed)

    async def result(self, market: VerifiedMarket) -> tuple[int, str] | None:
        ticker = market.ticker
        data, _ = await self.get("/markets/" + quote(ticker, safe=""))
        raw = data["market"]
        if raw.get("ticker") != ticker or raw.get("status") != "finalized":
            return None
        if raw.get("result") not in ("yes", "no"):
            raise ValueError("Unsupported/non-binary outcome; manual review required.")
        series, received = await self.get("/series/" + SERIES)
        if verify_market(raw, series["series"], now=received, finalized=True) != market:
            raise ValueError("Finalized market rules or strike changed; manual review required.")
        self.last_result = {"series": series["series"], "market": raw}
        return (
            100 if raw["result"] == "yes" else 0,
            "https://demo-api.kalshi.co/trade-api/v2/markets/" + quote(ticker, safe=""),
        )


def _best(levels: list[list[str]]) -> tuple[int, int, Decimal]:
    if not levels:
        raise ValueError("Missing executable book side.")
    parsed = []
    for level in levels:
        if len(level) != 2:
            raise ValueError("Malformed orderbook level.")
        try:
            price, size = map(Decimal, level)
        except InvalidOperation as exc:
            raise ValueError("Invalid fixed-point number.") from exc
        if not price.is_finite() or not size.is_finite() or not 0 < price < 1 or size < 0:
            raise ValueError("Invalid price/depth.")
        if size > 0:
            parsed.append((price, size))
    if not parsed:
        raise ValueError("No displayed liquidity.")
    best_price = max(p for p, _ in parsed)
    quantity = sum((s for p, s in parsed if p == best_price), Decimal(0))
    # Round bids downward, implied asks upward, and fractional contracts downward.
    cents = int((best_price * 100).to_integral_value(rounding=ROUND_FLOOR))
    count = int(quantity.to_integral_value(rounding=ROUND_FLOOR))
    if not 1 <= cents <= 99 or count < 4:
        raise ValueError("Insufficient whole-contract depth or unsupported extreme price.")
    return cents, count, best_price


def orderbook_quote(body: dict[str, Any], ticker: str, received: float) -> PaperQuote:
    book = body["orderbook_fp"]
    yes_bid, yes_size, raw_yes = _best(book["yes_dollars"])
    no_bid, no_size, raw_no = _best(book["no_dollars"])
    if raw_yes + raw_no > 1:
        raise ValueError("Crossed complementary book before cent rounding.")
    fingerprint = hashlib.sha256(json.dumps(book, sort_keys=True).encode()).hexdigest()[:16]
    return PaperQuote(
        quote_id=f"{ticker}:{received:.6f}:{fingerprint}",
        ticker=ticker,
        observed_at=received,
        yes_bid=yes_bid,
        yes_ask=100 - no_bid,
        no_bid=no_bid,
        no_ask=100 - yes_bid,
        yes_bid_size=yes_size,
        yes_ask_size=no_size,
        no_bid_size=no_size,
        no_ask_size=yes_size,
    )


class CoinbaseReference:
    """A proxy feed only; never used to infer or settle a Kalshi outcome."""

    URL = "https://api.exchange.coinbase.com/products/BTC-USD/ticker"

    def __init__(self) -> None:
        self.client = httpx.AsyncClient(timeout=2, follow_redirects=False, trust_env=False)

    async def tick(self) -> ReferenceTick:
        require_simulation()
        started = time.time()
        response = await self.client.get(self.URL, headers={"Cache-Control": "no-cache"})
        received = validate_response(response, started)
        body = response.json()
        tick = ReferenceTick(timestamp=utc_timestamp(body["time"]), price=float(body["price"]))
        bid, ask = float(body["bid"]), float(body["ask"])
        if (
            not 0 <= received - tick.timestamp <= 5
            or not all(math.isfinite(p) for p in (bid, ask))
            or not 0 < bid <= ask
            or (ask - bid) / tick.price > 0.001
            or abs(tick.price - (bid + ask) / 2) / tick.price > 0.001
        ):
            raise ValueError("Stale reference trade or invalid/wide reference spread.")
        return tick

    async def close(self) -> None:
        await self.client.aclose()
