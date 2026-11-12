"""Read-only PAPER-ledger reconciliation into the disabled execution journal.

No exchange API, broker construction, submissions, fills or account mutations.
The adapter is deliberately NOT exchange reconciliation. It audits complete
paper snapshots against the journal, immutable receipts and verified Kalshi
settlement evidence, then atomically releases staging risk only when flat.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_bot.btc_automation import ExecutionJournal
from kalshi_bot.btc_dashboard import connect_readonly
from kalshi_bot.btc_rules import utc_timestamp, verify_market
from kalshi_bot.exchange.models import OrderRequest
from kalshi_bot.safety import require_simulation


def _canonical(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False)


def reconcile_paper(
    journal: ExecutionJournal,
    ledger: Path,
    ticker: str,
    *,
    now: float,
    final_market: dict[str, Any] | None = None,
    series: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Idempotent restart/cancel/partial-fill/exit/settlement reconciliation.

    Input must be the separately approved future paper trial ledger. No command
    schedules this function today. Incomplete/unknown/conflicting state retains
    risk and arms the sticky kill. Killing does not guess fills or liquidate.
    """
    require_simulation()
    if isinstance(now, bool) or not math.isfinite(now) or now <= 0:
        raise ValueError("Invalid reconciliation time.")
    if ledger.resolve() == Path(journal.db.execute("PRAGMA database_list").fetchone()[2]).resolve():
        raise ValueError("Journal and paper ledger must be separate.")
    journal.db.executescript("""
        CREATE TABLE IF NOT EXISTS auto_paper_receipts(
            kind TEXT NOT NULL, receipt_id TEXT NOT NULL, canonical TEXT NOT NULL,
            PRIMARY KEY(kind,receipt_id)
        );
        CREATE TABLE IF NOT EXISTS auto_paper_snapshots(
            ticker TEXT PRIMARY KEY, observed_at REAL NOT NULL, canonical TEXT NOT NULL
        );
    """)
    try:
        with closing(connect_readonly(ledger)) as db:
            db.execute("BEGIN")
            account = dict(db.execute("SELECT * FROM account WHERE id=1").fetchone())
            if account["initial_cash"] != 5000 or account["cash"] < 0:
                raise ValueError("Unexpected paper account.")
            flows = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN action='buy' THEN -quantity*price-fee "
                "ELSE quantity*price-fee END),0),COALESCE(SUM(fee),0) FROM fills"
            ).fetchone()
            paid = db.execute("SELECT COALESCE(SUM(payout),0) FROM settlements").fetchone()[0]
            carried = db.execute("SELECT COALESCE(SUM(quantity*unit_cost),0) FROM lots").fetchone()[
                0
            ]
            if (
                account["cash"] != 5000 + flows[0] + paid
                or account["fees"] != flows[1]
                or account["realized_pnl"] != account["cash"] + carried - 5000
            ):
                raise ValueError("Paper account cash, fee or P&L reconciliation failed.")
            orders = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM orders WHERE ticker=? ORDER BY created,rowid", (ticker,)
                )
            ]
            fills = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM fills WHERE ticker=? ORDER BY timestamp,id", (ticker,)
                )
            ]
            lots = [
                dict(r)
                for r in db.execute("SELECT * FROM lots WHERE ticker=? ORDER BY id", (ticker,))
            ]
            settled = db.execute("SELECT * FROM settlements WHERE ticker=?", (ticker,)).fetchone()
            settlement = dict(settled) if settled is not None else None
            foreign = db.execute(
                "SELECT ticker FROM orders WHERE status='pending' AND ticker!=? "
                "UNION SELECT ticker FROM lots WHERE quantity>0 AND ticker!=?",
                (ticker, ticker),
            ).fetchall()
        with journal.transaction():
            intent = journal.db.execute(
                "SELECT * FROM auto_intents WHERE ticker=?", (ticker,)
            ).fetchone()
            if intent is None:
                raise ValueError("Unknown intent; never guess submission success.")
            if foreign:
                raise ValueError("Unexpected account exposure in another market.")
            if not orders:
                raise ValueError("Submission not found; retain risk, do not retry blindly.")
            order_map = {r["id"]: r for r in orders}
            if intent["client_id"] not in order_map:
                raise ValueError("Reserved client ID not found.")
            if any(o["created"] > now for o in orders):
                raise ValueError("Future order.")
            buy_orders = [o for o in orders if o["action"] == "buy"]
            if len(buy_orders) != 1 or buy_orders[0]["id"] != intent["client_id"]:
                raise ValueError("Duplicate/unexpected entry.")
            entry_request = OrderRequest.model_validate_json(buy_orders[0]["payload"])
            proposal = json.loads(intent["payload"])
            if (
                entry_request.count != proposal["count"]
                or entry_request.limit_price != proposal["limit_cents"]
            ):
                raise ValueError("Entry differs from reserved proposal.")
            inventory, cost, proceeds, fees = 0, 0, 0, 0
            for fill in fills:
                o = order_map.get(fill["order_id"])
                if (
                    o is None
                    or fill["side"] != intent["side"]
                    or fill["side"] != o["side"]
                    or fill["action"] != o["action"]
                    or fill["action"] not in ("buy", "sell")
                    or type(fill["quantity"]) is not int
                    or fill["quantity"] <= 0
                    or type(fill["price"]) is not int
                    or not 1 <= fill["price"] <= 99
                    or fill["fee"] != fill["quantity"] * 2
                    or not o["created"] < fill["timestamp"] <= now
                ):
                    raise ValueError("Invalid paper fill receipt.")
                encoded = _canonical(fill)
                old = journal.db.execute(
                    "SELECT canonical FROM auto_paper_receipts WHERE kind='fill' AND receipt_id=?",
                    (str(fill["id"]),),
                ).fetchone()
                if old is not None and old[0] != encoded:
                    raise ValueError("Conflicting immutable fill.")
                journal.db.execute(
                    "INSERT OR IGNORE INTO auto_paper_receipts VALUES('fill',?,?)",
                    (str(fill["id"]), encoded),
                )
                quantity = fill["quantity"]
                fees += fill["fee"]
                if fill["action"] == "buy":
                    inventory += quantity
                    cost += quantity * fill["price"] + fill["fee"]
                else:
                    inventory -= quantity
                    proceeds += quantity * fill["price"] - fill["fee"]
                if inventory < 0:
                    raise ValueError("Oversold or missing fill history.")
            if (
                cost > intent["risk_cents"]
                or sum(f["quantity"] for f in fills if f["action"] == "buy") > 1
            ):
                raise ValueError("Entry risk or size exceeded.")
            for o in orders:
                order = OrderRequest.model_validate_json(o["payload"])
                quantity = sum(f["quantity"] for f in fills if f["order_id"] == o["id"])
                if (
                    o["side"] != intent["side"]
                    or order.side.value != o["side"]
                    or order.action.value != o["action"]
                    or order.ticker != ticker
                    or order.client_order_id != o["id"]
                    or o["status"] not in ("pending", "filled", "cancelled")
                    or type(o["remaining"]) is not int
                    or not 0 <= o["remaining"] <= order.count
                    or quantity + o["remaining"] != order.count
                    or (o["status"] == "filled" and o["remaining"] != 0)
                    or (o["status"] == "pending" and o["remaining"] == 0)
                ):
                    raise ValueError("Incomplete/inconsistent order and fill history.")
                expected_reserve = (
                    o["remaining"] * (order.limit_price + 2 if o["action"] == "buy" else 2)
                    if o["status"] == "pending"
                    else 0
                )
                if o["reserved"] != expected_reserve:
                    raise ValueError("Incorrect paper reservation.")
                if any(
                    (
                        f["price"] > order.limit_price
                        if o["action"] == "buy"
                        else f["price"] < order.limit_price
                    )
                    for f in fills
                    if f["order_id"] == o["id"]
                ):
                    raise ValueError("Fill violates order limit.")
            expected_lots = sum(r["quantity"] for r in lots)
            if any(r["quantity"] < 0 or r["side"] != intent["side"] for r in lots):
                raise ValueError("Invalid paper inventory.")
            if settlement is not None:
                if final_market is None or series is None:
                    raise ValueError("Raw authoritative finalized evidence required.")
                market = verify_market(final_market, series, now=now, finalized=True)
                payout = 100 if final_market["result"] == "yes" else 0
                if (
                    market.ticker != ticker
                    or market.rules_sha256 != json.loads(intent["payload"])["rules_sha256"]
                    or not market.closes_at <= utc_timestamp(final_market["settlement_ts"]) <= now
                    or Decimal(final_market["settlement_value_dollars"]) != Decimal(payout) / 100
                    or settlement["outcome"] != payout
                    or not market.closes_at <= settlement["timestamp"] <= now
                    or settlement["source"]
                    != "https://demo-api.kalshi.co/trade-api/v2/markets/" + ticker
                ):
                    raise ValueError("Settlement evidence mismatch.")
                side_payout = payout if intent["side"] == "yes" else 100 - payout
                if settlement["payout"] != inventory * side_payout or expected_lots != 0:
                    raise ValueError("Settlement payout/inventory mismatch.")
                proceeds += settlement["payout"]
                inventory = 0
            elif expected_lots != inventory:
                raise ValueError("Fill and inventory totals disagree.")
            pending = sum(o["status"] == "pending" for o in orders)
            result = {
                "ticker": ticker,
                "inventory": inventory,
                "pending_orders": pending,
                "entry_cost_cents": cost,
                "proceeds_cents": proceeds,
                "fees_cents": fees,
                "reconciled": inventory == 0 and pending == 0,
                "pnl_cents": proceeds - cost if inventory == 0 else None,
            }
            evidence = _canonical({"orders": orders, "fills": fills, "settlement": settlement})
            old_snapshot = journal.db.execute(
                "SELECT * FROM auto_paper_snapshots WHERE ticker=?", (ticker,)
            ).fetchone()
            if old_snapshot is not None:
                old_data = json.loads(old_snapshot["canonical"])
                if now < old_snapshot["observed_at"]:
                    raise ValueError("Snapshot clock moved backwards.")
                if not {f["id"] for f in old_data["fills"]} <= {f["id"] for f in fills}:
                    raise ValueError("Historical fill disappeared.")
                old_orders = {o["id"]: o for o in old_data["orders"]}
                for oid, old_order in old_orders.items():
                    new_order = order_map.get(oid)
                    if (
                        new_order is None
                        or new_order["remaining"] > old_order["remaining"]
                        or any(
                            new_order[k] != old_order[k]
                            for k in ("id", "payload", "ticker", "side", "action", "created")
                        )
                        or (
                            old_order["status"] != "pending"
                            and new_order["status"] != old_order["status"]
                        )
                    ):
                        raise ValueError("Order state regressed.")
                if old_data["settlement"] is not None and old_data["settlement"] != settlement:
                    raise ValueError("Settlement changed; manual correction review required.")
            previous = journal.db.execute(
                "SELECT * FROM auto_results WHERE ticker=?", (ticker,)
            ).fetchone()
            if previous is not None and (
                not result["reconciled"]
                or previous["evidence"] != evidence
                or previous["pnl_cents"] != result["pnl_cents"]
            ):
                raise ValueError("Previously closed intent changed.")
            journal.db.execute(
                "INSERT INTO auto_paper_snapshots VALUES(?,?,?) ON CONFLICT(ticker) "
                "DO UPDATE SET observed_at=excluded.observed_at,canonical=excluded.canonical",
                (ticker, now, evidence),
            )
            if result["reconciled"]:
                journal.db.execute(
                    "INSERT OR IGNORE INTO auto_results VALUES(?,?,?,?)",
                    (ticker, now, result["pnl_cents"], evidence),
                )
                journal.db.execute(
                    "UPDATE auto_intents SET status='reconciled' WHERE ticker=?", (ticker,)
                )
            if old_snapshot is None or old_snapshot["canonical"] != evidence:
                journal.event(
                    "paper_reconciliation",
                    {**result, "evidence_sha256": hashlib.sha256(evidence.encode()).hexdigest()},
                    now,
                )
            return result
    except (ValueError, TypeError, KeyError, ArithmeticError, sqlite3.Error, OSError) as exc:
        journal.kill("paper reconciliation incomplete/conflicting; risk retained", now)
        raise ValueError("Paper reconciliation failed closed; inspect evidence offline.") from exc
