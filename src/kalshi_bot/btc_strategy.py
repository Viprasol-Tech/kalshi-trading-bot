"""BTC 15-minute paper strategy with persistent one-entry-per-market budgets."""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

import httpx

from kalshi_bot.btc_data import CoinbaseReference, DemoBTCSource
from kalshi_bot.btc_model import (
    CalibrationArtifact,
    Estimate,
    ReferenceTick,
    calibration_check,
    estimate,
)
from kalshi_bot.btc_rules import VerifiedMarket
from kalshi_bot.exchange.models import Action, OrderRequest, Side
from kalshi_bot.paper import (
    FEE_PER_CONTRACT,
    MAX_TRADE_LOSS,
    SLIPPAGE_CENTS,
    PaperBroker,
    PaperQuote,
)
from kalshi_bot.safety import require_simulation


@dataclass(frozen=True)
class Entry:
    side: Side
    probability: float
    net_edge: float
    count: int
    limit_price: int
    maximum_loss: int


def select_entry(
    forecast: Estimate,
    quote: PaperQuote,
    *,
    haircut: float,
    available_cash: int,
) -> Entry | None:
    if not 0 <= forecast.yes_probability <= 1 or not 0.05 <= haircut <= 0.25:
        raise ValueError("Invalid forecast or calibration haircut.")
    choices = []
    for side in Side:
        ask = quote.yes_ask if side is Side.YES else quote.no_ask
        depth = quote.yes_ask_size if side is Side.YES else quote.no_ask_size
        bid = quote.yes_bid if side is Side.YES else quote.no_bid
        if ask is None or bid is None or not 1 <= bid <= ask <= 99:
            continue
        limit = ask + SLIPPAGE_CENTS
        if limit > 99:
            continue
        p = Decimal(str(forecast.yes_probability))
        probability = p if side is Side.YES else Decimal(1) - p
        lower = max(Decimal(0), probability - Decimal(str(haircut)))
        # Ask includes spread; slippage and estimated per-contract fees are explicit.
        unit_cost = limit + FEE_PER_CONTRACT
        edge = lower - Decimal(unit_cost) / Decimal(100)
        count = min(MAX_TRADE_LOSS // unit_cost, available_cash // unit_cost, depth // 4)
        if edge >= Decimal("0.08") and count >= 1:
            choices.append(
                Entry(side, float(probability), float(edge), count, limit, count * unit_cost)
            )
    return max(choices, key=lambda c: c.net_edge) if choices else None


class BTCPaperRunner:
    """Bounded runner; no production exchange access or real-order capability."""

    def __init__(
        self,
        broker: PaperBroker,
        source: DemoBTCSource,
        reference: CoinbaseReference,
        calibration: CalibrationArtifact | None = None,
        *,
        clock: Callable[[], float] | None = None,
        recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        require_simulation()
        self.broker, self.source, self.reference = broker, source, reference
        self.calibration = calibration
        self.clock = clock or time.time
        self.recorder = recorder
        self.db = broker.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS btc_reference (
                timestamp REAL PRIMARY KEY, price REAL NOT NULL, source TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS btc_markets (
                ticker TEXT PRIMARY KEY, metadata TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS btc_entries (
                ticker TEXT PRIMARY KEY, order_id TEXT NOT NULL,
                prediction TEXT NOT NULL, predicted_at REAL NOT NULL,
                outcome INTEGER
            );
            CREATE TABLE IF NOT EXISTS btc_equity (
                timestamp REAL NOT NULL, equity INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS btc_forecasts (
                ticker TEXT NOT NULL, horizon_bucket INTEGER NOT NULL,
                features TEXT NOT NULL, probability REAL NOT NULL,
                outcome INTEGER, resolved_at REAL, evidence TEXT,
                PRIMARY KEY(ticker,horizon_bucket)
            );
        """)

    def _record(self, kind: str, **data: Any) -> None:
        self.broker.record_event("btc_" + kind, data)
        if self.recorder is not None and kind == "skipped":
            self.recorder(
                {
                    "kind": "skip",
                    "observed_at": self.clock(),
                    "ticker": data.get("ticker"),
                    "reason": str(data["reason"]),
                }
            )

    def _remember(self, tick: ReferenceTick) -> list[ReferenceTick]:
        with self.broker._transaction():
            last = self.db.execute("SELECT MAX(timestamp) FROM btc_reference").fetchone()[0]
            if last is not None and tick.timestamp < last:
                raise ValueError("Reference feed moved backwards.")
            previous = self.db.execute(
                "SELECT price,source FROM btc_reference WHERE timestamp=?", (tick.timestamp,)
            ).fetchone()
            if previous is not None and (
                previous["price"] != tick.price or previous["source"] != tick.source
            ):
                raise ValueError("Conflicting reference tick at the same timestamp.")
            self.db.execute(
                "INSERT OR IGNORE INTO btc_reference VALUES(?,?,?)",
                (tick.timestamp, tick.price, tick.source),
            )
            self.db.execute(
                "DELETE FROM btc_reference WHERE timestamp<=?", (tick.timestamp - 1800,)
            )
        return [
            ReferenceTick(timestamp=r["timestamp"], price=r["price"], source=r["source"])
            for r in self.db.execute("SELECT * FROM btc_reference ORDER BY timestamp")
        ]

    def _entry(self, market: VerifiedMarket, forecast: Estimate, choice: Entry, now: float) -> None:
        # Enforce a single logical entry across restarts, including cancelled/unfilled attempts.
        # This prevents re-entry orders from silently exceeding the $1 per-market trade budget.
        if self.db.execute("SELECT 1 FROM btc_entries WHERE ticker=?", (market.ticker,)).fetchone():
            return
        order = OrderRequest(
            ticker=market.ticker,
            action=Action.BUY,
            side=choice.side,
            count=choice.count,
            client_order_id="btc15m:" + market.ticker,
            **(
                {"yes_price": choice.limit_price}
                if choice.side is Side.YES
                else {"no_price": choice.limit_price}
            ),
        )
        prediction = {
            **asdict(forecast),
            "net_edge": choice.net_edge,
            "side": choice.side.value,
            "rules_sha256": market.rules_sha256,
            "maximum_loss_cents": choice.maximum_loss,
        }
        # Persist intent BEFORE submission. A crash may cause a missed trade, never
        # an unrecorded or duplicate logical entry. Rejected attempts are not retried.
        with self.broker._transaction():
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO btc_entries VALUES(?,?,?,?,NULL)",
                (market.ticker, order.client_order_id, json.dumps(prediction), now),
            ).rowcount
        if inserted:
            oid = self.broker.submit(order, now=now)
            self._record("entry", ticker=market.ticker, order_id=oid, **prediction)

    async def _settle_known(self, now: float) -> None:
        rows = self.db.execute("SELECT metadata FROM btc_markets").fetchall()
        for row in rows:
            market = VerifiedMarket(**json.loads(row["metadata"]))
            missing_label = self.db.execute(
                "SELECT 1 FROM btc_entries WHERE ticker=? AND outcome IS NULL "
                "UNION ALL SELECT 1 FROM btc_forecasts WHERE ticker=? AND outcome IS NULL",
                (market.ticker, market.ticker),
            ).fetchone()
            if market.closes_at > now or (
                self.broker._settled(market.ticker) and missing_label is None
            ):
                continue
            self.broker.cancel_market(market.ticker)
            result = await self.source.result(market)
            if result is None:
                self._record("outcome_pending", ticker=market.ticker)
                continue
            payout, evidence = result
            confirmed_at = max(now, self.clock())
            if self.recorder is not None:
                self.recorder(
                    {
                        "kind": "outcome",
                        "observed_at": confirmed_at,
                        **self.source.last_result,
                    }
                )
            self.broker.settle(
                market.ticker,
                payout,
                source=evidence,
                confirmed=True,
                now=confirmed_at,
            )
            with self.broker._transaction():
                self.db.execute(
                    "UPDATE btc_entries SET outcome=? WHERE ticker=?",
                    (1 if payout == 100 else 0, market.ticker),
                )
                self.db.execute(
                    "UPDATE btc_forecasts SET outcome=?,resolved_at=?,evidence=? WHERE ticker=?",
                    (1 if payout == 100 else 0, confirmed_at, evidence, market.ticker),
                )
            self._record(
                "outcome", ticker=market.ticker, yes_outcome=payout // 100, source=evidence
            )

    def _cancel_all(self) -> None:
        for row in self.db.execute("SELECT ticker FROM btc_markets").fetchall():
            self.broker.cancel_market(row["ticker"])

    async def step(self) -> None:
        require_simulation()
        now = self.clock()
        markets, skipped = await self.source.discover()
        for skip in skipped:
            self._record("skipped", **skip)
        if not markets:
            self._record("skipped", reason="no verified active demo BTC 15-minute markets")
        tick = await self.reference.tick()
        reference_received_at = self.clock()
        if self.recorder is not None:
            self.recorder(
                {
                    "kind": "reference",
                    "observed_at": reference_received_at,
                    "reference": tick.model_dump(),
                }
            )
        ticks = self._remember(tick)
        await self._settle_known(self.clock())
        marks: dict[str, PaperQuote] = {}
        active = {m.ticker for m in markets}
        for row in self.db.execute("SELECT ticker FROM btc_markets").fetchall():
            if row["ticker"] not in active:
                self.broker.cancel_market(row["ticker"])
        for market in markets:
            try:
                with self.broker._transaction():
                    known = self.db.execute(
                        "SELECT metadata FROM btc_markets WHERE ticker=?", (market.ticker,)
                    ).fetchone()
                    metadata = json.dumps(asdict(market), sort_keys=True)
                    if known is not None and known["metadata"] != metadata:
                        raise ValueError("Previously verified market metadata changed.")
                    self.db.execute(
                        "INSERT OR IGNORE INTO btc_markets VALUES(?,?)", (market.ticker, metadata)
                    )
                current, book = await self.source.refresh(market)
                now = self.clock()
                if not self.broker.quote_is_usable(book, now=now):
                    raise ValueError("Stale or repeated executable book.")
                marks[market.ticker] = book
                if self.recorder is not None:
                    self.recorder(
                        {
                            "kind": "frame",
                            "observed_at": now,
                            "reference_received_at": reference_received_at,
                            "reference": tick.model_dump(),
                            "quote": book.model_dump(),
                            **self.source.last_metadata,
                        }
                    )
                forecast = estimate(ticks, current.strike, current.closes_at, now=now)
                # Preserve the FIRST forecast in each horizon bucket, including rejected
                # signals. Future evaluation can use a predeclared sample rather than
                # cherry-picking winning entry forecasts or losing reference history.
                features = {
                    "ticker": current.ticker,
                    "rules_sha256": current.rules_sha256,
                    "strike": current.strike,
                    "predicted_at": now,
                    "closes_at": current.closes_at,
                    "ticks": [t.model_dump() for t in ticks],
                }
                with self.broker._transaction():
                    self.db.execute(
                        "INSERT OR IGNORE INTO btc_forecasts "
                        "(ticker,horizon_bucket,features,probability) VALUES(?,?,?,?)",
                        (
                            current.ticker,
                            int(forecast.seconds_to_close // 180),
                            json.dumps(features),
                            forecast.yes_probability,
                        ),
                    )
                gate = calibration_check(
                    self.calibration,
                    now=now,
                    horizon=current.closes_at - now,
                    yes_probability=forecast.yes_probability,
                )
                self._record("signal", ticker=market.ticker, **asdict(forecast), calibration=gate)
                if not gate["passed"]:
                    raise ValueError(str(gate["reason"]))
                # Add already-reserved entry funds back only for evaluating an existing order.
                # New submissions still use actual available cash in the broker.
                cash = self.broker.account()["available_cash"]
                reserved = sum(
                    r["reserved"]
                    for r in self.broker.rows("orders")
                    if r["ticker"] == market.ticker and r["status"] == "pending"
                )
                choice = select_entry(
                    forecast,
                    book,
                    haircut=gate["probability_haircut"],
                    available_cash=cash + reserved,
                )
                if choice is None:
                    raise ValueError(
                        "net edge below 8 points, missing liquidity or insufficient cash"
                    )
                existing = self.broker.pending(market.ticker)
                if existing and any(o.side is not choice.side for o in existing):
                    raise ValueError("Signal side changed; cancel pending entry")
                # Recheck pending orders at their actual limit, not a cheaper current ask.
                if any(
                    Decimal(str(choice.probability))
                    - Decimal(str(gate["probability_haircut"]))
                    - Decimal(o.limit_price + FEE_PER_CONTRACT) / 100
                    < Decimal("0.08")
                    for o in existing
                ):
                    raise ValueError("Existing limit no longer meets required net edge")
                pending_times = [
                    r["created"]
                    for r in self.broker.rows("orders")
                    if r["ticker"] == market.ticker and r["status"] == "pending"
                ]
                if pending_times and book.observed_at - min(pending_times) < 1:
                    self._record(
                        "skipped",
                        ticker=market.ticker,
                        reason="minimum one-second fill latency not reached",
                    )
                    continue
                self.broker.process_quote(book, now=now)
                self._entry(current, forecast, choice, now)
            except (ValueError, KeyError, TypeError) as exc:
                self.broker.cancel_market(market.ticker)
                self._record("skipped", ticker=market.ticker, reason=str(exc))
        with self.broker._transaction():
            self.db.execute(
                "INSERT INTO btc_equity VALUES(?,?)", (self.clock(), self.broker.equity(marks))
            )

    async def run(self, ticks: int = 1, interval: float = 2) -> dict[str, Any]:
        if not 1 <= ticks <= 1800 or not math.isfinite(interval) or not 1 <= interval <= 5:
            raise ValueError("Use 1-1800 ticks with a 1-5 second interval.")
        try:
            for index in range(ticks):
                try:
                    await self.step()
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                    self._cancel_all()
                    self._record("skipped", reason=str(exc), category="data_or_validation_error")
                    with self.broker._transaction():
                        self.db.execute(
                            "INSERT INTO btc_equity VALUES(?,?)",
                            (self.clock(), self.broker.equity({})),
                        )
                if index + 1 < ticks:
                    await asyncio.sleep(interval)
        finally:
            self._cancel_all()
        return self.report()

    def report(self) -> dict[str, Any]:
        entries = self.db.execute("SELECT * FROM btc_entries").fetchall()
        settled = [r for r in entries if r["outcome"] is not None]
        filled_oids = {r["order_id"] for r in self.broker.rows("fills")}
        closed = [r for r in settled if r["order_id"] in filled_oids]
        pnl_by_market = {r["ticker"]: r["realized_pnl"] for r in self.broker.rows("settlements")}
        wins = sum(pnl_by_market.get(r["ticker"], 0) > 0 for r in closed)
        curve = [self.broker.account()["initial_cash"]] + [
            r[0] for r in self.db.execute("SELECT equity FROM btc_equity ORDER BY timestamp")
        ]
        peak, drawdown = curve[0], 0.0
        for equity in curve:
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
        errors = [
            (json.loads(r["prediction"])["yes_probability"] - r["outcome"]) ** 2 for r in settled
        ]
        forecast_rows = self.db.execute(
            "SELECT * FROM btc_forecasts WHERE outcome IS NOT NULL "
            "ORDER BY ticker,horizon_bucket DESC"
        ).fetchall()
        first_by_market: dict[str, Any] = {}
        for row in forecast_rows:
            first_by_market.setdefault(row["ticker"], row)
        forecast_errors = [(r["probability"] - r["outcome"]) ** 2 for r in first_by_market.values()]
        ece = None
        if first_by_market:
            ece = 0.0
            for bucket in range(5):
                group = [
                    r
                    for r in first_by_market.values()
                    if min(4, int(r["probability"] * 5)) == bucket
                ]
                if group:
                    ece += abs(sum(r["probability"] - r["outcome"] for r in group)) / len(
                        first_by_market
                    )
        win_interval = None
        if closed:
            n, z = len(closed), 1.959963984540054
            p = wins / n
            denominator = 1 + z * z / n
            center = (p + z * z / (2 * n)) / denominator
            half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
            win_interval = [max(0, center - half), min(1, center + half)]
        return {
            "mode": "demo-paper-only",
            "account": self.broker.account(),
            "pnl_scope": "shared simulated account",
            "equity_mark_policy": (
                "fresh bid less exit costs; missing/error marks value inventory at zero"
            ),
            "entry_attempts": len(entries),
            "settled_filled_markets": len(closed),
            "win_rate": wins / len(closed) if closed else None,
            "win_rate_95pct_wilson_interval": win_interval,
            "net_liquidation_pnl_cents": curve[-1] - curve[0],
            "maximum_drawdown": drawdown,
            "outcome_sample_size": len(settled),
            "observed_brier": sum(errors) / len(errors) if errors else None,
            "forecast_resolved_market_sample": len(first_by_market),
            "forecast_brier": (
                sum(forecast_errors) / len(forecast_errors) if forecast_errors else None
            ),
            "forecast_ece_5_bins": ece,
            "calibration_loaded": self.calibration is not None,
            "calibration_status": (
                "requires empirical gate on every signal"
                if self.calibration is not None
                else "not loaded; entries blocked"
            ),
            "reference_samples": self.db.execute("SELECT COUNT(*) FROM btc_reference").fetchone()[
                0
            ],
            "recent_skips": [
                json.loads(r["data"])
                for r in self.db.execute(
                    "SELECT data FROM events WHERE kind='btc_skipped' ORDER BY id DESC LIMIT 5"
                )
            ],
            "profitability_established": False,
        }
