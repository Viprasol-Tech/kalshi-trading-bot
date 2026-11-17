"""Disabled automatic BTC execution staging; no background jobs or network clients.

The planner is usable for observation diagnostics. Dispatch is sealed by a
non-configurable release gate, independently of calibration and feature flags.
The journal is NOT a paper brokerage or bankroll. Reservations model future
exposure; they never create simulated fills or modify the $50 ledger.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from kalshi_bot.btc_model import (
    CalibrationArtifact,
    ReferenceTick,
    calibration_check,
    estimate,
)
from kalshi_bot.btc_observe import book_fields
from kalshi_bot.btc_rules import verify_market
from kalshi_bot.btc_validate import executable_edges
from kalshi_bot.exchange.models import Action, OrderRequest, Side
from kalshi_bot.paper import PaperBroker
from kalshi_bot.safety import require_entry_release, require_simulation

# Fixed policy, not environment overrides: $2 daily gross loss including
# reserved risk, distinct from the $1 per-trade worst-case loss cap.
MAX_RISK_CENTS = 100
DAILY_LOSS_CENTS = 200
MIN_EDGE = Decimal("0.08")
SLIPPAGE_CENTS = 2
FEE_RESERVE_CENTS = 2
PARTICIPATION_DIVISOR = 4


@dataclass(frozen=True)
class Opportunity:
    ticker: str
    observed_at: float
    rules_sha256: str
    yes_probability: float | None
    yes_edge: float | None
    no_edge: float | None
    side: str | None
    limit_cents: int | None
    count: int
    maximum_loss_cents: int
    blockers: tuple[str, ...]

    @property
    def client_id(self) -> str:
        # One logical attempt per market, not per side/tick/restart.
        return "btc-auto:" + hashlib.sha256(self.ticker.encode()).hexdigest()[:32]


@dataclass(frozen=True)
class ExitPlan:
    ticker: str
    side: str
    observed_at: float
    limit_cents: int | None
    rules_sha256: str
    blockers: tuple[str, ...]

    @property
    def client_id(self) -> str:
        return "btc-exit:" + hashlib.sha256(self.ticker.encode()).hexdigest()[:32]


def exit_plan(
    payload: dict[str, Any], series: dict[str, Any], side: str, *, now: float
) -> ExitPlan:
    """Staged policy: attempt ONE pre-close sale during the final 60 seconds.

    No price guess, stop-loss fill or compulsory liquidation. If fresh executable
    liquidity is absent, retain risk and await authoritative finalized settlement.
    This pure plan neither reads inventory nor places an order.
    """
    ticker, rules, limit, blockers = "unverified", "", None, []
    try:
        require_simulation()
        market = verify_market(payload["market_metadata"], series, now=now)
        ticker, rules = market.ticker, market.rules_sha256
        if side not in ("yes", "no") or not 0 < market.closes_at - now <= 60:
            raise ValueError("No pre-close exit due.")
        strict = executable_edges({**payload, "yes_probability": 0.5}, now)
        if not strict["strict_execution_eligible"]:
            raise ValueError(str(strict["strict_edge_reason"]))
        book = book_fields({"orderbook_fp": payload["raw_book"]})
        bid = book[side + "_bid"]
        if bid is None or book[side + "_bid_depth"] < PARTICIPATION_DIVISOR:
            raise ValueError("Insufficient executable exit bid/depth.")
        limit = int((Decimal(str(bid)) * 100).to_integral_value(rounding=ROUND_FLOOR)) - 2
        if not FEE_RESERVE_CENTS <= limit <= 99:
            raise ValueError("Exit cannot increase loss beyond the fully prepaid entry.")
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        blockers.append(str(exc))
        limit = None
    return ExitPlan(ticker, side, now, limit, rules, tuple(blockers))


def opportunity(
    payload: dict[str, Any],
    series: dict[str, Any],
    ticks: list[ReferenceTick],
    calibration: CalibrationArtifact | None,
    *,
    now: float,
    available_cents: int,
) -> Opportunity:
    """Reverify rules, recompute probability and evaluate BOTH executable asks.

    No caller-supplied probability, `passed` flag or edge is trusted. All numbers
    use conservative cent rounding. Require four displayed whole contracts to
    model at most 25% participation; missing bid ladders block this first release.
    """
    ticker = str(payload.get("market_metadata", {}).get("ticker", "unverified"))
    p: float | None = None
    edges: dict[str, float | None] = {"yes": None, "no": None}
    digest = ""
    blockers: list[str] = []
    choices: list[tuple[Decimal, str, int, int, int]] = []
    try:
        require_simulation()
        if (
            isinstance(now, bool)
            or not math.isfinite(now)
            or now <= 0
            or type(available_cents) is not int
            or available_cents < 0
        ):
            raise ValueError("Invalid clock or available cash.")
        market = verify_market(payload["market_metadata"], series, now=now)
        digest = market.rules_sha256
        forecast = estimate(ticks, market.strike, market.closes_at, now=now)
        p = forecast.yes_probability
        checked = {**payload, "yes_probability": p}
        strict = executable_edges(checked, now)
        if not strict["strict_execution_eligible"]:
            raise ValueError(str(strict["strict_edge_reason"]))
        # Verify both ladders explicitly. One-sided data remains observation data.
        raw = payload["raw_book"]
        if not raw.get("yes_dollars") or not raw.get("no_dollars"):
            raise ValueError("One-sided book; first execution release requires both sides.")
        gate = calibration_check(
            calibration, now=now, horizon=market.closes_at - now, yes_probability=p
        )
        haircut = Decimal(str(gate.get("probability_haircut", 0.05)))
        if not gate["passed"]:
            blockers.append("calibration: " + str(gate["reason"]))
        decimal_p = Decimal(str(p))
        for side, probability in (("yes", decimal_p), ("no", Decimal(1) - decimal_p)):
            if strict["strict_" + side + "_net_edge"] is None:
                continue
            ask = Decimal(str(payload[side + "_ask"]))
            limit = int((ask * 100).to_integral_value(rounding=ROUND_CEILING)) + SLIPPAGE_CENTS
            if not 1 <= limit <= 99:
                continue
            # One contract maximum: avoids multi-contract aggregate-fee rounding
            # ambiguity and limits worst-case purchase + reserved fee to <=$1.
            fee = max(
                FEE_RESERVE_CENTS,
                math.ceil(0.07 * (limit / 100) * (1 - limit / 100) * 100),
            )
            cost = limit + fee
            net = probability - haircut - Decimal(cost) / 100
            edges[side] = float(net)
            depth = Decimal(str(payload[side + "_ask_depth"]))
            if (
                net >= MIN_EDGE
                and cost <= min(MAX_RISK_CENTS, available_cents)
                and depth >= PARTICIPATION_DIVISOR
            ):
                choices.append((net, side, limit, 1, cost))
        if not choices:
            blockers.append("No side meets edge, full-contract depth, cash and risk limits.")
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        blockers.append(str(exc))
        edges = {"yes": None, "no": None}
        choices.clear()
    choice = max(choices, default=None)
    return Opportunity(
        ticker,
        now,
        digest,
        p,
        edges["yes"],
        edges["no"],
        choice[1] if choice and not blockers else None,
        choice[2] if choice and not blockers else None,
        choice[3] if choice and not blockers else 0,
        choice[4] if choice and not blockers else 0,
        tuple(blockers),
    )


class ExecutionJournal:
    """Crash-persistent idempotency, global single exposure and sticky kill latch.

    `claim` is staging, not execution. No SQL here credits/debits an account.
    Terminal intents are retained permanently to prevent same-market re-entry.
    Unknown submission status retains its risk and requires reconciliation.
    Daily budget uses UTC gross realized losses (gains never replenish it), plus
    worst-case reserved risk. Default kill latch is armed. No reset API exists.
    """

    def __init__(self, path: Path) -> None:
        require_simulation()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS auto_control(
                id INTEGER PRIMARY KEY CHECK(id=1), killed INTEGER NOT NULL,
                reason TEXT NOT NULL
            );
            INSERT OR IGNORE INTO auto_control VALUES(1,1,'initial disabled release');
            CREATE TABLE IF NOT EXISTS auto_intents(
                ticker TEXT PRIMARY KEY, client_id TEXT UNIQUE NOT NULL,
                created REAL NOT NULL, side TEXT NOT NULL, risk_cents INTEGER NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_events(
                id INTEGER PRIMARY KEY, at REAL NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_results(
                ticker TEXT PRIMARY KEY REFERENCES auto_intents(ticker),
                confirmed_at REAL NOT NULL, pnl_cents INTEGER NOT NULL, evidence TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_exits(
                ticker TEXT PRIMARY KEY,client_id TEXT UNIQUE NOT NULL,
                status TEXT NOT NULL,payload TEXT NOT NULL
            );
        """)
        self.db.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, kind: str, payload: dict[str, Any], at: float) -> None:
        if isinstance(at, bool) or not math.isfinite(at) or at <= 0:
            raise ValueError("Invalid audit timestamp.")
        self.db.execute(
            "INSERT INTO auto_events(at,kind,payload) VALUES(?,?,?)",
            (at, kind, json.dumps(payload, sort_keys=True, allow_nan=False)),
        )

    def kill(self, reason: str, at: float) -> None:
        with self.transaction():
            self.event("kill", {"reason": reason}, at)
            self.db.execute("UPDATE auto_control SET killed=1,reason=? WHERE id=1", (reason,))

    def analyze(
        self,
        payload: dict[str, Any],
        series: dict[str, Any],
        ticks: list[ReferenceTick],
        calibration: CalibrationArtifact | None,
        *,
        now: float,
        available_cents: int,
    ) -> Opportunity:
        """Observation callback for a future supervised collector, never dispatches.

        Store accepted AND rejected diagnostics; do not cherry-pick entry signals.
        The active observation collector remains independent of this journal.
        """
        decision = opportunity(
            payload, series, ticks, calibration, now=now, available_cents=available_cents
        )
        self.event("opportunity_diagnostic", asdict(decision), now)
        return decision

    def claim(self, decision: Opportunity, *, now: float, cash_cents: int) -> None:
        """Atomic staging reservation; safe under two competing worker processes."""
        if (
            decision.blockers
            or decision.side not in ("yes", "no")
            or decision.count != 1
            or decision.limit_cents is None
            or not 1 <= decision.maximum_loss_cents <= MAX_RISK_CENTS
            or decision.maximum_loss_cents < decision.limit_cents + FEE_RESERVE_CENTS
            or type(cash_cents) is not int
            or cash_cents < decision.maximum_loss_cents
            or not math.isfinite(now)
            or not 0 <= now - decision.observed_at <= 1
        ):
            raise ValueError("Invalid or expired reservation proposal.")
        with self.transaction():
            control = self.db.execute("SELECT killed FROM auto_control WHERE id=1").fetchone()
            if control is None or control["killed"]:
                raise ValueError("Sticky kill switch is armed.")
            if self.db.execute(
                "SELECT 1 FROM auto_intents WHERE ticker=?", (decision.ticker,)
            ).fetchone():
                raise ValueError("Duplicate market intent.")
            if self.db.execute("SELECT 1 FROM auto_intents WHERE status!='reconciled'").fetchone():
                raise ValueError("Global overlapping exposure or unreconciled intent.")
            day = int(now // 86400) * 86400
            loss = self.db.execute(
                "SELECT COALESCE(SUM(MAX(0,-pnl_cents)),0) FROM auto_results "
                "WHERE confirmed_at>=? AND confirmed_at<?",
                (day, day + 86400),
            ).fetchone()[0]
            if loss + decision.maximum_loss_cents > DAILY_LOSS_CENTS:
                raise ValueError("Daily gross-loss budget including reserved risk exceeded.")
            self.db.execute(
                "INSERT INTO auto_intents VALUES(?,?,?,?,?,'reserved',?)",
                (
                    decision.ticker,
                    decision.client_id,
                    now,
                    decision.side,
                    decision.maximum_loss_cents,
                    json.dumps(asdict(decision), allow_nan=False),
                ),
            )
            self.event("intent_reserved", asdict(decision), now)

    def dispatch(self, decision: Opportunity, broker: PaperBroker, *, now: float) -> str | None:
        """Future paper adapter, sealed. Never create a broker implicitly.

        Crash between reservation and acknowledgement leaves a blocking intent.
        No automatic retry, fill, or risk release is possible in this build.
        """
        self.event("dispatch_blocked", {"ticker": decision.ticker, "release": False}, now)
        require_entry_release()
        if broker.slippage_cents != SLIPPAGE_CENTS:
            raise ValueError("Automatic paper trial requires immutable two-cent slippage.")
        # Deliberately unreachable until a separately reviewed release changes the gate.
        if broker.pending() or broker.db.execute("SELECT 1 FROM lots WHERE quantity>0").fetchone():
            raise ValueError("Existing account exposure; reconciliation required before dispatch.")
        self.claim(decision, now=now, cash_cents=broker.account()["available_cash"])
        assert decision.limit_cents is not None and decision.side is not None
        order = OrderRequest(
            ticker=decision.ticker,
            action=Action.BUY,
            side=Side(decision.side),
            count=decision.count,
            client_order_id=decision.client_id,
            **{decision.side + "_price": decision.limit_cents},
        )
        try:
            oid = broker.submit(order, now=now)
            with self.transaction():
                self.db.execute(
                    "UPDATE auto_intents SET status=? WHERE ticker=?",
                    ("submitted" if oid else "rejected_pending_reconciliation", decision.ticker),
                )
                self.event("submission_ack", {"ticker": decision.ticker, "order_id": oid}, now)
            return oid
        except BaseException:
            self.kill("uncertain submission; manual reconciliation required", now)
            raise

    def dispatch_exit(self, plan: ExitPlan, broker: PaperBroker, *, now: float) -> str | None:
        """Sealed sell adapter: one durable intent; uncertainty never triggers retry."""
        require_entry_release()
        if (
            plan.blockers
            or plan.side not in ("yes", "no")
            or plan.limit_cents is None
            or not 2 <= plan.limit_cents <= 99
            or not 0 <= now - plan.observed_at <= 1
            or broker.slippage_cents != SLIPPAGE_CENTS
            or broker.holdings(plan.ticker, Side(plan.side)) != 1
            or broker.pending()
        ):
            raise ValueError("Invalid, stale or unreconciled exit.")
        with self.transaction():
            intent = self.db.execute(
                "SELECT * FROM auto_intents WHERE ticker=?", (plan.ticker,)
            ).fetchone()
            has_snapshot = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='auto_paper_snapshots'"
            ).fetchone()
            snapshot = (
                self.db.execute(
                    "SELECT observed_at FROM auto_paper_snapshots WHERE ticker=?", (plan.ticker,)
                ).fetchone()
                if has_snapshot
                else None
            )
            if (
                intent is None
                or intent["status"] != "submitted"
                or snapshot is None
                or not 0 <= now - snapshot[0] <= 5
                or intent["side"] != plan.side
                or json.loads(intent["payload"])["rules_sha256"] != plan.rules_sha256
                or self.db.execute("SELECT killed FROM auto_control").fetchone()[0]
                or self.db.execute(
                    "SELECT 1 FROM auto_exits WHERE ticker=?", (plan.ticker,)
                ).fetchone()
            ):
                raise ValueError("Killed, duplicate or unknown exit intent.")
            self.db.execute(
                "INSERT INTO auto_exits VALUES(?,?,'prepared',?)",
                (plan.ticker, plan.client_id, json.dumps(asdict(plan))),
            )
        try:
            oid = broker.submit(
                OrderRequest(
                    ticker=plan.ticker,
                    side=Side(plan.side),
                    action=Action.SELL,
                    count=1,
                    client_order_id=plan.client_id,
                    **{plan.side + "_price": plan.limit_cents},
                ),
                now=now,
            )
            with self.transaction():
                self.db.execute(
                    "UPDATE auto_exits SET status=? WHERE ticker=?",
                    ("submitted" if oid else "rejected_pending_review", plan.ticker),
                )
                self.event("exit_ack", {"ticker": plan.ticker, "order_id": oid}, now)
            return oid
        except BaseException:
            self.kill("uncertain exit; inventory/risk retained; no automatic retry", now)
            raise
