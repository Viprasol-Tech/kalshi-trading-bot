"""Read-only demo quote timestamp investigation, never an execution feed.

No connection occurs on import/construction. Network use requires a separate
explicit approval argument. Legacy authenticated clients and entry locks are
unchanged. This adapter cannot send arbitrary commands, orders or account queries.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa
from websockets.legacy.client import Connect

from kalshi_bot.btc_rules import utc_timestamp
from kalshi_bot.exchange.auth import build_auth_headers
from kalshi_bot.safety import SimulationOnlyError, require_simulation

DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"
WS_PATH = "/trade-api/ws/v2"


class NoRedirectConnect(Connect):
    def handle_redirect(self, uri: str) -> None:
        # websockets otherwise follows redirects and can forward custom auth headers.
        raise SimulationOnlyError("Demo WebSocket redirects are forbidden.")


def subscription(tickers: list[str]) -> dict[str, Any]:
    if (
        not 1 <= len(tickers) <= 16
        or len(set(tickers)) != len(tickers)
        or any(not re.fullmatch(r"KXBTC15M-[A-Z0-9-]{1,80}", t) for t in tickers)
    ):
        raise ValueError("One to sixteen distinct BTC demo market tickers required.")
    return {
        "id": 1,
        "cmd": "subscribe",
        "params": {
            "channels": ["orderbook_delta"],
            "market_tickers": tickers,
            "update_interval_ms": 0,
            # Explicitly keep NO prices denominated in NO dollars.
            "use_yes_price": False,
        },
    }


class ResnapshotRequired(ValueError):
    """Discard ALL books in this session and reconnect for fresh snapshots."""


@dataclass(frozen=True)
class QuoteEvent:
    ticker: str
    sid: int
    seq: int
    clock: int
    kind: str
    event_at: float | None
    event_field: str | None
    sending_at: float | None
    received_at: float
    received_monotonic: float
    event_fresh: bool
    # A delta timestamp describes ONE level, never every resting level.
    whole_book_freshness_verified: bool = False


class DemoBookSession:
    """One connection's sequence and exact decimal resting book state.

    Missing snapshot sides remain empty. Snapshots have no invented per-level
    timestamps. Sequence gaps, replay, clock regression or malformed data clear
    every book and require a new connection. Equal clocks are NOT deduplicated.
    """

    def __init__(self, tickers: list[str]) -> None:
        subscription(tickers)
        self.tickers = frozenset(tickers)
        self.seq: int | None = None
        self.sid: int | None = None
        self.books: dict[str, dict[str, dict[Decimal, Decimal]]] = {}
        self.clocks: dict[str, int] = {}
        self.snapshot_clocks: dict[str, int] = {}
        self.last_received: float | None = None
        self.last_monotonic: float | None = None
        self.failed = False

    @staticmethod
    def integer(value: Any) -> int:
        if type(value) is not int or not 0 <= value < 2**63:
            raise ValueError("Invalid exchange sequence, clock or timestamp.")
        return value

    def consume(self, raw: dict[str, Any], *, received_at: float, monotonic: float) -> QuoteEvent:
        try:
            if self.failed:
                raise ValueError("Session already invalidated.")
            if (
                type(received_at) not in (int, float)
                or type(monotonic) not in (int, float)
                or not math.isfinite(received_at)
                or received_at <= 0
                or not math.isfinite(monotonic)
                or monotonic < 0
            ):
                raise ValueError("Invalid receive clocks.")
            if self.last_received is not None and self.last_monotonic is not None:
                wall_elapsed = received_at - self.last_received
                mono_elapsed = monotonic - self.last_monotonic
                if wall_elapsed < 0 or mono_elapsed < 0 or abs(wall_elapsed - mono_elapsed) > 2:
                    raise ValueError("Receive clock regression or discontinuity.")
            if not isinstance(raw, dict) or not isinstance(raw.get("msg"), dict):
                raise ValueError("Malformed book envelope.")
            kind = raw["type"]
            if kind not in ("orderbook_snapshot", "orderbook_delta"):
                raise ValueError("Only public book messages are accepted.")
            sid, seq = self.integer(raw["sid"]), self.integer(raw["seq"])
            if sid < 1 or seq < 1:
                raise ValueError("Invalid subscription identity.")
            if self.seq is not None and (sid != self.sid or seq != self.seq + 1):
                raise ValueError("Subscription sequence gap/replay.")
            msg = raw["msg"]
            ticker, clock = msg["market_ticker"], self.integer(msg["clock"])
            if ticker not in self.tickers:
                raise ValueError("Unexpected market.")
            if clock < self.clocks.get(ticker, 0):
                raise ValueError("Exchange maintenance/clock regression.")
            event_at, field = None, None
            if kind == "orderbook_snapshot":
                book: dict[str, dict[Decimal, Decimal]] = {"yes": {}, "no": {}}
                for side in ("yes", "no"):
                    levels = msg.get(side + "_dollars_fp", [])
                    if not isinstance(levels, list) or len(levels) > 10000:
                        raise ValueError("Invalid/beyond-bound snapshot levels.")
                    for level in levels:
                        if not isinstance(level, list) or len(level) != 2:
                            raise ValueError("Invalid level.")
                        if any(not isinstance(v, str) for v in level):
                            raise ValueError("Fixed-point levels must be strings.")
                        p, q = Decimal(level[0]), Decimal(level[1])
                        if (
                            not p.is_finite()
                            or not q.is_finite()
                            or not 0 < p < 1
                            or not 0 < q <= Decimal("1e12")
                            or p in book[side]
                        ):
                            raise ValueError("Invalid price/depth.")
                        book[side][p] = q
                self.books[ticker] = book
                self.snapshot_clocks[ticker] = clock
            else:
                if ticker not in self.books:
                    raise ValueError("Delta before snapshot.")
                if clock <= self.snapshot_clocks[ticker]:
                    raise ValueError("Delta clock must be after its replacement snapshot.")
                side = msg["side"]
                if side not in ("yes", "no"):
                    raise ValueError("Unknown side.")
                if any(not isinstance(msg[k], str) for k in ("price_dollars", "delta_fp")):
                    raise ValueError("Fixed-point delta must use strings.")
                p, delta = Decimal(msg["price_dollars"]), Decimal(msg["delta_fp"])
                if not p.is_finite() or not delta.is_finite() or not 0 < p < 1:
                    raise ValueError("Invalid delta.")
                level_qty = self.books[ticker][side].get(p, Decimal(0)) + delta
                if not 0 <= level_qty <= Decimal("1e12"):
                    raise ValueError("Negative resting depth.")
                if level_qty:
                    self.books[ticker][side][p] = level_qty
                else:
                    self.books[ticker][side].pop(p, None)
                if msg.get("ts_ms") is not None:
                    event_at, field = self.integer(msg["ts_ms"]) / 1000, "msg.ts_ms"
                elif msg.get("ts") is not None:
                    event_at, field = utc_timestamp(msg["ts"]), "msg.ts"
                if (
                    msg.get("ts_ms") is not None
                    and msg.get("ts") is not None
                    and utc_timestamp(msg["ts"]) != event_at
                ):
                    raise ValueError("Conflicting delta event timestamps.")
            sending = raw.get("sending_ts_ms")
            sending_at = self.integer(sending) / 1000 if sending is not None else None
            if event_at is not None and (
                event_at <= 0
                or event_at > received_at
                or (sending_at is not None and sending_at < event_at)
            ):
                raise ValueError("Invalid event/receive ordering.")
            if sending_at is not None and not 0 < sending_at <= received_at:
                raise ValueError("Future/invalid send timestamp.")
            sides = self.books[ticker]
            if sides["yes"] and sides["no"] and max(sides["yes"]) + max(sides["no"]) > 1:
                raise ValueError("Crossed complementary book.")
            self.sid, self.seq, self.clocks[ticker] = sid, seq, clock
            self.last_received, self.last_monotonic = received_at, monotonic
            return QuoteEvent(
                ticker,
                sid,
                seq,
                clock,
                kind,
                event_at,
                field,
                sending_at,
                received_at,
                monotonic,
                event_at is not None and 0 <= received_at - event_at <= 5,
            )
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError) as exc:
            self.failed = True
            self.books.clear()
            self.clocks.clear()
            self.snapshot_clocks.clear()
            raise ResnapshotRequired(
                "Book session invalid; reconnect and replace all books."
            ) from exc


async def stream(
    tickers: list[str],
    *,
    demo_key_id: str,
    demo_private_key: rsa.RSAPrivateKey,
    approved: bool = False,
    duration_seconds: int = 60,
) -> AsyncIterator[QuoteEvent]:
    """Future bounded demo probe; no generic endpoint or outbound command API.

    Credentials must be separately verified as DEMO by the operator; key bytes
    alone cannot prove provenance. No keys, headers, raw messages or exceptions
    are logged. No automatic reconnect/deadline extension or persistent collector.
    """
    require_simulation()
    if approved is not True:
        raise SimulationOnlyError("Separate demo feed connection approval required.")
    if type(duration_seconds) is not int or not 1 <= duration_seconds <= 300:
        raise ValueError("Demo probe must last at most five minutes.")
    command = subscription(tickers)
    if not demo_key_id or not isinstance(demo_private_key, rsa.RSAPrivateKey):
        raise ValueError("Separately verified demo credentials required.")
    headers = build_auth_headers(
        demo_key_id, demo_private_key, int(time.time() * 1000), "GET", WS_PATH
    )
    deadline = time.monotonic() + duration_seconds
    session = DemoBookSession(tickers)
    quiet_logger = logging.Logger("kalshi-demo-probe-disabled-logging")
    quiet_logger.disabled = True
    quiet_logger.propagate = False
    # No redirect handling, user-supplied URL, account channel or order method.
    async with (
        asyncio.timeout(duration_seconds),
        NoRedirectConnect(
            DEMO_WS_URL,
            extra_headers=headers,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=2,
            max_size=1_048_576,
            logger=quiet_logger,
        ) as ws,
    ):
        await ws.send(json.dumps(command))
        while time.monotonic() < deadline:
            message = json.loads(await ws.recv())
            if message.get("type") in ("subscribed", "ok"):
                continue
            if message.get("type") == "error":
                raise ValueError("Demo feed rejected subscription; details withheld.")
            yield session.consume(message, received_at=time.time(), monotonic=time.monotonic())
