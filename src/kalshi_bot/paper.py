"""Persistent, long-only YES/NO paper brokerage. No exchange write methods.

Fills require a *later*, fresh quote and visible opposing liquidity. The
conservative model uses a two-cent estimated fee per filled contract, one-cent
adverse slippage, and at most 25% of displayed depth per quote. These are
explicit simulation assumptions, not a verified exchange fee schedule.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kalshi_bot.exchange.models import Action, OrderRequest, Position, Side, TimeInForce
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.safety import require_entry_release, require_simulation

INITIAL_CASH = 5000
MAX_TRADE_LOSS = 100
FEE_PER_CONTRACT = 2
SLIPPAGE_CENTS = 1
MAX_QUOTE_AGE = 5.0


class PaperQuote(BaseModel):
    """Executable side-specific prices and depth observed at a known time."""

    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    quote_id: str = Field(min_length=1)
    ticker: str = Field(min_length=1)
    observed_at: float = Field(gt=0)
    yes_bid: int | None = Field(default=None, ge=1, le=99)
    yes_ask: int | None = Field(default=None, ge=1, le=99)
    no_bid: int | None = Field(default=None, ge=1, le=99)
    no_ask: int | None = Field(default=None, ge=1, le=99)
    yes_bid_size: int = Field(default=0, ge=0)
    yes_ask_size: int = Field(default=0, ge=0)
    no_bid_size: int = Field(default=0, ge=0)
    no_ask_size: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _uncrossed(self) -> PaperQuote:
        for side in ("yes", "no"):
            bid, ask = getattr(self, f"{side}_bid"), getattr(self, f"{side}_ask")
            if bid is not None and ask is not None and bid > ask:
                raise ValueError("Crossed quote: fail closed.")
        return self


class PaperBroker:
    """SQLite ledger with atomic cash/inventory reservations and FIFO costs.

    Separate YES and NO holdings are funded purchases, never naked shorts.
    A settlement may be supplied only as an explicit confirmed result; this
    component never guesses a result from a price or from expiration.
    """

    def __init__(
        self,
        path: str | Path = "data/paper.db",
        *,
        starting_cash: int = INITIAL_CASH,
        risk: RiskManager | None = None,
        offline_backtest: bool = False,
        slippage_cents: int = SLIPPAGE_CENTS,
    ) -> None:
        require_simulation()
        if type(slippage_cents) is not int or slippage_cents not in (1, 2):
            raise ValueError("Supported paper slippage is one or two cents.")
        self._slippage_cents = slippage_cents
        existed = str(path) != ":memory:" and Path(path).exists()
        if offline_backtest and str(path) != ":memory:":
            raise ValueError("Offline backtests must use an isolated in-memory ledger.")
        self._offline_backtest = offline_backtest
        if type(starting_cash) is not int or not 0 < starting_cash <= INITIAL_CASH:
            raise ValueError("Paper account must start with at most $50.")
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.risk = risk or RiskManager()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS account (
                id INTEGER PRIMARY KEY CHECK(id=1), initial_cash INTEGER NOT NULL,
                cash INTEGER NOT NULL CHECK(cash>=0),
                realized_pnl INTEGER NOT NULL DEFAULT 0,
                fees INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                ticker TEXT NOT NULL, side TEXT NOT NULL, action TEXT NOT NULL,
                created REAL NOT NULL, remaining INTEGER NOT NULL CHECK(remaining>=0),
                reserved INTEGER NOT NULL CHECK(reserved>=0), status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lots (
                id INTEGER PRIMARY KEY, ticker TEXT NOT NULL, side TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK(quantity>=0), unit_cost INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fills (
                id INTEGER PRIMARY KEY, order_id TEXT NOT NULL REFERENCES orders(id),
                ticker TEXT NOT NULL, side TEXT NOT NULL, action TEXT NOT NULL,
                quantity INTEGER NOT NULL, price INTEGER NOT NULL, fee INTEGER NOT NULL,
                timestamp REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS settlements (
                ticker TEXT PRIMARY KEY, outcome INTEGER NOT NULL,
                payout INTEGER NOT NULL, realized_pnl INTEGER NOT NULL,
                source TEXT NOT NULL, timestamp REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS quotes (
                id TEXT PRIMARY KEY, ticker TEXT NOT NULL, timestamp REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, kind TEXT NOT NULL,
                data TEXT NOT NULL
            );
        """)
        try:
            with self._transaction():
                policy = self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_policy'"
                ).fetchone()
                if policy:
                    saved = self.db.execute(
                        "SELECT slippage FROM paper_policy WHERE id=1"
                    ).fetchone()
                    if saved is None or saved[0] != slippage_cents:
                        raise ValueError("Immutable paper slippage policy mismatch.")
                elif slippage_cents != SLIPPAGE_CENTS:
                    if existed:
                        raise ValueError("Use a new separate ledger for two-cent paper trials.")
                    if any(
                        self.db.execute(f"SELECT 1 FROM {t}").fetchone()
                        for t in (
                            "orders",
                            "fills",
                            "lots",
                            "settlements",
                        )
                    ):
                        raise ValueError("A new paper trial requires a separate empty ledger.")
                    self.db.execute(
                        "CREATE TABLE paper_policy("
                        "id INTEGER PRIMARY KEY CHECK(id=1),slippage INTEGER)"
                    )
                    self.db.execute("INSERT INTO paper_policy VALUES(1,?)", (slippage_cents,))
                self.db.execute(
                    "INSERT OR IGNORE INTO account(id,initial_cash,cash) VALUES(1,?,?)",
                    (starting_cash, starting_cash),
                )
                if self.account()["initial_cash"] != starting_cash:
                    raise ValueError("Existing paper account cannot be reset or re-funded.")
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    @property
    def slippage_cents(self) -> int:
        return self._slippage_cents

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        require_simulation()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            if self.account()["available_cash"] < 0:
                raise RuntimeError("Paper ledger invariant failed; transaction rolled back.")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _event(self, kind: str, data: dict[str, Any], now: float) -> None:
        self.db.execute(
            "INSERT INTO events(timestamp,kind,data) VALUES(?,?,?)",
            (now, kind, json.dumps(data, sort_keys=True, allow_nan=False)),
        )

    def record_event(self, kind: str, data: dict[str, Any]) -> None:
        with self._transaction():
            self._event(kind, data, time.time())

    @staticmethod
    def _now(now: float | None) -> float:
        value = time.time() if now is None else now
        if not math.isfinite(value) or value <= 0:
            raise ValueError("A finite positive timestamp is required.")
        return value

    def account(self) -> dict[str, int]:
        row = self.db.execute("SELECT * FROM account WHERE id=1").fetchone()
        if row is None:
            raise RuntimeError("Paper account is missing.")
        reserved = int(
            self.db.execute(
                "SELECT COALESCE(SUM(reserved),0) FROM orders WHERE status='pending'"
            ).fetchone()[0]
        )
        return {
            "initial_cash": row["initial_cash"],
            "cash": row["cash"],
            "reserved_cash": reserved,
            "available_cash": row["cash"] - reserved,
            "realized_pnl": row["realized_pnl"],
            "fees": row["fees"],
        }

    def holdings(self, ticker: str, side: Side) -> int:
        return int(
            self.db.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM lots WHERE ticker=? AND side=?",
                (ticker, side.value),
            ).fetchone()[0]
        )

    def positions(self) -> dict[str, Position]:
        result: dict[str, Position] = {}
        for row in self.db.execute(
            "SELECT ticker, SUM(CASE WHEN side='yes' THEN quantity ELSE -quantity END) AS net "
            "FROM lots WHERE quantity>0 GROUP BY ticker"
        ):
            result[row["ticker"]] = Position(ticker=row["ticker"], position=row["net"])
        return result

    def pending(self, ticker: str | None = None) -> list[OrderRequest]:
        rows = self.db.execute(
            "SELECT * FROM orders WHERE status='pending' AND (? IS NULL OR ticker=?)",
            (ticker, ticker),
        ).fetchall()
        return [
            OrderRequest.model_validate({**json.loads(r["payload"]), "count": r["remaining"]})
            for r in rows
        ]

    def _settled(self, ticker: str) -> bool:
        return (
            self.db.execute("SELECT 1 FROM settlements WHERE ticker=?", (ticker,)).fetchone()
            is not None
        )

    def quote_is_usable(self, quote: PaperQuote, *, now: float) -> bool:
        latest = self.db.execute(
            "SELECT MAX(timestamp) FROM quotes WHERE ticker=?", (quote.ticker,)
        ).fetchone()[0]
        return (
            0 <= now - quote.observed_at <= MAX_QUOTE_AGE
            and (latest is None or quote.observed_at > latest)
            and not self.db.execute("SELECT 1 FROM quotes WHERE id=?", (quote.quote_id,)).fetchone()
            and not self._settled(quote.ticker)
        )

    def submit(self, order: OrderRequest, *, now: float | None = None) -> str | None:
        """Reserve funds only. Submission itself never creates a fill."""
        self._require_entries()
        timestamp = self._now(now)
        # Revalidate even if a caller used pydantic's unchecked model_construct.
        order = OrderRequest.model_validate(order.model_dump())
        with self._transaction():
            reason = ""
            cash = self.account()["available_cash"]
            pos = self.positions().get(order.ticker, Position(ticker=order.ticker)).position
            fees = FEE_PER_CONTRACT * order.count
            if self._settled(order.ticker):
                reason = "market already settled"
            elif order.time_in_force is TimeInForce.FILL_OR_KILL:
                reason = "fill-or-kill simulation not supported"
            elif order.action is Action.BUY:
                loss = (order.limit_price + FEE_PER_CONTRACT) * order.count
                if loss > MAX_TRADE_LOSS:
                    reason = "worst-case entry loss including fees exceeds $1"
            else:
                reserved_units = sum(
                    o.count
                    for o in self.pending(order.ticker)
                    if o.action is Action.SELL and o.side is order.side
                )
                if order.count > self.holdings(order.ticker, order.side) - reserved_units:
                    reason = "naked shorts and overselling reserved holdings are disabled"
                elif order.limit_price <= FEE_PER_CONTRACT:
                    reason = "exit proceeds must cover estimated exit fees"
            decision = self.risk.check(
                order,
                pos,
                available_cash=cash,
                pending_orders=self.pending(order.ticker),
                estimated_fees=fees,
            )
            if not decision.approved:
                reason = reason or decision.reason
            if (
                order.client_order_id
                and self.db.execute(
                    "SELECT 1 FROM orders WHERE id=?", (order.client_order_id,)
                ).fetchone()
            ):
                reason = "duplicate client order id"
            if reason:
                self._event("rejected", {"order": order.to_payload(), "reason": reason}, timestamp)
                return None
            oid = order.client_order_id or str(uuid.uuid4())
            reserve = order.limit_price * order.count + fees if order.action is Action.BUY else fees
            self.db.execute(
                "INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    oid,
                    order.model_dump_json(),
                    order.ticker,
                    order.side.value,
                    order.action.value,
                    timestamp,
                    order.count,
                    reserve,
                    "pending",
                ),
            )
            self._event(
                "submitted",
                {"order_id": oid, "order": order.to_payload(), "reserved_cash": reserve},
                timestamp,
            )
            return oid

    def cancel(self, oid: str, *, now: float | None = None) -> None:
        with self._transaction():
            self._cancel(oid, self._now(now))

    def _cancel(self, oid: str, now: float) -> None:
        row = self.db.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
        if row is None:
            raise ValueError("Unknown paper order.")
        if row["status"] == "pending":
            self.db.execute("UPDATE orders SET status='cancelled', reserved=0 WHERE id=?", (oid,))
            self._event("cancelled", {"order_id": oid}, now)

    def cancel_market(self, ticker: str, *, now: float | None = None) -> None:
        with self._transaction():
            for row in self.db.execute(
                "SELECT id FROM orders WHERE ticker=? AND status='pending'", (ticker,)
            ).fetchall():
                self._cancel(row["id"], self._now(now))

    def process_quote(self, quote: PaperQuote, *, now: float | None = None) -> int:
        """Fill at most 25% of depth, only on a later fresh quote. Return units."""
        self._require_entries()
        timestamp = self._now(now)
        quote = PaperQuote.model_validate(quote.model_dump())
        with self._transaction():
            if not self.quote_is_usable(quote, now=timestamp):
                self._event(
                    "quote_skipped",
                    {"quote_id": quote.quote_id, "reason": "stale/future/replayed/settled"},
                    timestamp,
                )
                return 0
            self.db.execute(
                "INSERT INTO quotes VALUES(?,?,?)",
                (quote.quote_id, quote.ticker, quote.observed_at),
            )
            depth: dict[tuple[str, str], int] = {}
            for side in ("yes", "no"):
                for book_side in ("bid", "ask"):
                    depth[side, book_side] = getattr(quote, f"{side}_{book_side}_size") // 4
            filled = 0
            rows = self.db.execute(
                "SELECT * FROM orders WHERE ticker=? AND status='pending' ORDER BY created,rowid",
                (quote.ticker,),
            ).fetchall()
            for row in rows:
                if quote.observed_at <= row["created"]:
                    continue
                order = OrderRequest.model_validate_json(row["payload"])
                side = order.side.value
                buying = order.action is Action.BUY
                book_side = "ask" if buying else "bid"
                bid, ask = getattr(quote, f"{side}_bid"), getattr(quote, f"{side}_ask")
                price = None
                if bid is not None and ask is not None:
                    price = ask + self.slippage_cents if buying else bid - self.slippage_cents
                qty = 0
                if (
                    price is not None
                    and 1 <= price <= 99
                    and (price <= order.limit_price if buying else price >= order.limit_price)
                    and not order.post_only
                ):
                    qty = min(row["remaining"], depth[side, book_side])
                # Passive queue position is unknown: post-only orders never receive assumed fills.
                if qty > 0 and price is not None:
                    self._fill(row, order, qty, price, timestamp)
                    filled += qty
                    depth[side, book_side] -= qty
                    # YES asks and NO bids (and vice versa) share economic liquidity.
                    other_side = "no" if side == "yes" else "yes"
                    other_book = "bid" if book_side == "ask" else "ask"
                    depth[other_side, other_book] = max(0, depth[other_side, other_book] - qty)
                else:
                    self._event(
                        "unfilled", {"order_id": row["id"], "quote_id": quote.quote_id}, timestamp
                    )
                if order.time_in_force is TimeInForce.IMMEDIATE_OR_CANCEL:
                    self._cancel(row["id"], timestamp)
            return filled

    def _consume(self, ticker: str, side: str, quantity: int) -> int:
        cost = 0
        remaining = quantity
        for lot in self.db.execute(
            "SELECT * FROM lots WHERE ticker=? AND side=? AND quantity>0 ORDER BY id",
            (ticker, side),
        ).fetchall():
            take = min(lot["quantity"], remaining)
            cost += take * lot["unit_cost"]
            self.db.execute("UPDATE lots SET quantity=quantity-? WHERE id=?", (take, lot["id"]))
            remaining -= take
            if not remaining:
                break
        if remaining:
            raise RuntimeError("Inventory invariant failed.")
        return cost

    def _fill(
        self,
        row: sqlite3.Row,
        order: OrderRequest,
        qty: int,
        price: int,
        now: float,
    ) -> None:
        fee = qty * FEE_PER_CONTRACT
        if order.action is Action.BUY:
            self.db.execute(
                "UPDATE account SET cash=cash-?, fees=fees+? WHERE id=1", (qty * price + fee, fee)
            )
            self.db.execute(
                "INSERT INTO lots(ticker,side,quantity,unit_cost) VALUES(?,?,?,?)",
                (order.ticker, order.side.value, qty, price + FEE_PER_CONTRACT),
            )
        else:
            cost = self._consume(order.ticker, order.side.value, qty)
            proceeds = qty * price - fee
            self.db.execute(
                "UPDATE account SET cash=cash+?, realized_pnl=realized_pnl+?, fees=fees+? "
                "WHERE id=1",
                (proceeds, proceeds - cost, fee),
            )
        remaining = row["remaining"] - qty
        reserve = remaining * (
            order.limit_price + FEE_PER_CONTRACT if order.action is Action.BUY else FEE_PER_CONTRACT
        )
        self.db.execute(
            "UPDATE orders SET remaining=?, reserved=?, status=? WHERE id=?",
            (remaining, reserve, "pending" if remaining else "filled", row["id"]),
        )
        self.db.execute(
            "INSERT INTO fills(order_id,ticker,side,action,quantity,price,fee,timestamp) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (row["id"], order.ticker, order.side.value, order.action.value, qty, price, fee, now),
        )
        self._event(
            "fill", {"order_id": row["id"], "quantity": qty, "price": price, "fee": fee}, now
        )

    def settle(
        self,
        ticker: str,
        yes_payout_cents: int,
        *,
        source: str,
        confirmed: bool = False,
        now: float | None = None,
    ) -> int:
        """Apply an explicitly confirmed binary result, once. No inferred settlement."""
        if not confirmed or type(yes_payout_cents) is not int or yes_payout_cents not in (0, 100):
            raise ValueError(
                "Confirmed binary settlement required; non-binary outcomes unsupported."
            )
        if not source.strip():
            raise ValueError("A settlement evidence reference is required.")
        timestamp = self._now(now)
        with self._transaction():
            previous = self.db.execute(
                "SELECT outcome FROM settlements WHERE ticker=?", (ticker,)
            ).fetchone()
            if previous is not None:
                if previous["outcome"] != yes_payout_cents:
                    raise ValueError("Conflicting settlement; manual review required.")
                return 0
            for row in self.db.execute(
                "SELECT id FROM orders WHERE ticker=? AND status='pending'", (ticker,)
            ).fetchall():
                self._cancel(row["id"], timestamp)
            payout, cost = 0, 0
            for side in Side:
                quantity = self.holdings(ticker, side)
                payout += quantity * (
                    yes_payout_cents if side is Side.YES else 100 - yes_payout_cents
                )
                cost += self._consume(ticker, side.value, quantity) if quantity else 0
            pnl = payout - cost
            self.db.execute(
                "UPDATE account SET cash=cash+?, realized_pnl=realized_pnl+? WHERE id=1",
                (payout, pnl),
            )
            self.db.execute(
                "INSERT INTO settlements VALUES(?,?,?,?,?,?)",
                (ticker, yes_payout_cents, payout, pnl, source, timestamp),
            )
            self._event(
                "settlement",
                {"ticker": ticker, "payout": payout, "realized_pnl": pnl, "source": source},
                timestamp,
            )
            return payout

    def equity(self, quotes: dict[str, PaperQuote]) -> int:
        """Cash plus conservative liquidation value (bid less exit fees/slippage).

        Missing marks value inventory at zero, never at a guessed last price.
        """
        equity = self.account()["cash"]
        for row in self.db.execute(
            "SELECT ticker,side,SUM(quantity) AS quantity FROM lots GROUP BY ticker,side"
        ):
            quote = quotes.get(row["ticker"])
            bid = getattr(quote, row["side"] + "_bid") if quote is not None else None
            equity += row["quantity"] * max(0, (bid or 0) - self.slippage_cents - FEE_PER_CONTRACT)
        return equity

    def _require_entries(self) -> None:
        if not self._offline_backtest:
            require_entry_release()

    def rows(self, table: str) -> list[dict[str, Any]]:
        if table not in {"orders", "fills", "settlements", "events", "lots"}:
            raise ValueError("Unknown ledger table.")
        return [dict(r) for r in self.db.execute(f"SELECT * FROM {table} ORDER BY rowid")]
