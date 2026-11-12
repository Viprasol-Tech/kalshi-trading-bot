"""Multi-category PAPER state machine. No exchange order client or live mode.

Default model registry is empty; reviewed category models/data do not exist yet.
Tests patch the release boundary for explicitly synthetic temporary-ledger drills.
The BTC strategy, its journal and its 400-market campaign remain independent.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from kalshi_bot.btc_rules import utc_timestamp
from kalshi_bot.safety import SimulationOnlyError, require_simulation


class Category(str, Enum):
    CRYPTO = "crypto"
    SPORTS = "sports"
    ECONOMICS = "economics"
    WEATHER = "weather"
    POLITICS = "politics"
    FINANCIAL = "financial"


CATEGORY_NAMES = {
    "crypto": Category.CRYPTO,
    "cryptocurrency": Category.CRYPTO,
    "sports": Category.SPORTS,
    "economics": Category.ECONOMICS,
    "weather": Category.WEATHER,
    "climate and weather": Category.WEATHER,
    "politics": Category.POLITICS,
    "elections": Category.POLITICS,
    "financials": Category.FINANCIAL,
    "financial": Category.FINANCIAL,
}


POLICY = {
    "version": 1,
    "starting_cash": 5000,
    "risk": 100,
    "daily_gross_loss": 200,
    "open_positions": 3,
    "category_positions": 1,
    "correlated_positions": 1,
    "net_edge": 0.08,
    "slippage_cents": 2,
    "minimum_fee_cents": 2,
}


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def finite(value: float) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def rules_digest(raw: dict[str, Any], series: dict[str, Any]) -> str:
    primary = raw.get("rules_primary")
    sources = series.get("settlement_sources")
    if not isinstance(primary, str) or not primary.strip() or not sources:
        raise ValueError("Missing authoritative resolution rules/sources.")
    identity = {
        key: raw.get(key)
        for key in (
            "ticker",
            "event_ticker",
            "open_time",
            "close_time",
            "floor_strike",
            "cap_strike",
            "strike_type",
            "custom_strike",
            "yes_sub_title",
        )
    }
    return digest(
        [
            primary,
            raw.get("rules_secondary", ""),
            sources,
            identity,
            {key: series.get(key) for key in ("ticker", "category", "fee_type", "fee_multiplier")},
        ]
    )


@dataclass(frozen=True)
class Contract:
    ticker: str
    event: str
    category: Category
    opens: float
    closes: float
    rules: str
    metadata_at: float
    fee_multiplier: float
    raw: dict[str, Any]
    series: dict[str, Any]

    @classmethod
    def verify(
        cls,
        raw: dict[str, Any],
        series: dict[str, Any],
        *,
        at: float,
        now: float,
        event_metadata: dict[str, Any] | None = None,
    ) -> Contract:
        require_simulation()
        if not finite(now) or not finite(at) or not 0 <= now - at <= 60:
            raise ValueError("Stale market metadata.")
        category = CATEGORY_NAMES.get(str(series.get("category", "")).strip().lower())
        ticker, event, prefix = raw.get("ticker"), raw.get("event_ticker"), series.get("ticker")
        if not isinstance(ticker, str) or not isinstance(event, str) or not isinstance(prefix, str):
            raise ValueError("Missing market/series identity.")
        linked = (
            event_metadata.get("event_ticker") == event
            and event_metadata.get("series_ticker") == prefix
            if event_metadata is not None
            else event.startswith(prefix + "-") and ticker.startswith(event + "-")
        )
        if (
            category is None
            or not ticker
            or not event
            or not prefix
            or not linked
            or raw.get("status") not in ("open", "active")
            or series.get("fee_type") != "quadratic"
        ):
            raise ValueError("Unverified category, identity, status or fee schedule.")
        multiplier = series.get("fee_multiplier")
        if (
            not isinstance(multiplier, (int, float))
            or isinstance(multiplier, bool)
            or not math.isfinite(multiplier)
            or not 0 < multiplier <= 10
        ):
            raise ValueError("Unsupported/missing authoritative fee multiplier.")
        opens, closes = utc_timestamp(raw["open_time"]), utc_timestamp(raw["close_time"])
        if not opens <= now < closes:
            raise ValueError("Market is not open.")
        # Keep the BTC 15-minute floor requirement; never infer it from prices/title.
        if prefix == "KXBTC15M":
            strike = raw.get("floor_strike")
            if (
                not isinstance(strike, (int, float))
                or isinstance(strike, bool)
                or not math.isfinite(strike)
                or strike <= 0
            ):
                raise ValueError("Missing/unverified BTC floor strike.")
            from kalshi_bot.btc_rules import verify_market

            verify_market(raw, series, now=now)
        return cls(
            ticker,
            event,
            category,
            opens,
            closes,
            rules_digest(raw, series),
            at,
            float(multiplier),
            json.loads(canonical(raw)),
            json.loads(canonical(series)),
        )


@dataclass(frozen=True)
class Book:
    ticker: str
    quote_id: str
    event_at: float
    received_at: float
    yes_bid: float | None
    no_bid: float | None
    yes_depth: float
    no_depth: float

    def check(self, *, now: float) -> None:
        values = (self.event_at, self.received_at, now)
        if not self.quote_id or not all(finite(v) for v in values):
            raise ValueError("Missing actual quote-event/receipt timestamps.")
        if not 0 <= now - self.event_at <= 5 or not 0 <= now - self.received_at <= 5:
            raise ValueError("Stale/future quote.")
        if not 0 <= self.received_at - self.event_at <= 2:
            raise ValueError("Transport latency outside policy.")
        if any(
            not isinstance(v, (int, float))
            or isinstance(v, bool)
            or not math.isfinite(v)
            or not 0 < v < 1
            for v in (self.yes_bid, self.no_bid)
        ) or any(
            type(v) not in (int, float) or not math.isfinite(v) or v < 4
            for v in (self.yes_depth, self.no_depth)
        ):
            raise ValueError("One-sided/missing book or insufficient real depth.")
        assert self.yes_bid is not None and self.no_bid is not None
        spread = 1 - self.yes_bid - self.no_bid
        if not 0 <= spread <= 0.10:
            raise ValueError("Crossed or excessively wide executable spread.")

    def bid(self, side: str) -> Decimal:
        value = self.yes_bid if side == "yes" else self.no_bid
        if value is None:
            raise ValueError("Missing bid.")
        return Decimal(str(value))

    def ask(self, side: str) -> Decimal:
        return Decimal(1) - self.bid("no" if side == "yes" else "yes")


@dataclass(frozen=True)
class Forecast:
    probability: float
    uncertainty: float
    at: float
    model_id: str
    groups: tuple[str, ...]


class ProbabilityModel(Protocol):
    """Reviewed plugin must implement category rules AND calibrated inference.

    No API is provided for loading a `validated:true` or a supplied probability
    from configuration. External reference and validation data are model-specific.
    """

    model_id: str

    def predict(self, contract: Contract, *, now: float) -> Forecast: ...


def reviewed_models() -> dict[Category, ProbabilityModel]:
    """No validated multi-market plugins have been delivered or reviewed yet."""
    return {}


def require_model_release(category: Category, model_id: str) -> None:
    require_simulation()
    raise SimulationOnlyError(
        f"{category.value}/{model_id}: no reviewed model, outcome provenance, "
        "chronological validation and paper release. A caller's passed flag is not evidence."
    )


def fee(price_cents: int, multiplier: float) -> int:
    p = Decimal(price_cents) / 100
    amount = Decimal("0.07") * Decimal(str(multiplier)) * p * (1 - p) * 100
    return max(2, int(amount.to_integral_value(rounding=ROUND_CEILING)))


@dataclass(frozen=True)
class Decision:
    ticker: str
    side: str | None
    limit: int | None
    risk: int
    edge: float | None
    forecast: Forecast | None
    reason: str


def rank(contract: Contract, book: Book, model: ProbabilityModel | None, *, now: float) -> Decision:
    forecast = None
    try:
        book.check(now=now)
        if (
            book.ticker != contract.ticker
            or not 0 <= now - contract.metadata_at <= 60
            or not contract.opens <= now < contract.closes
        ):
            raise ValueError("Quote/metadata identity, freshness or market rollover mismatch.")
        if model is None:
            raise ValueError("No category-specific validated model.")
        require_model_release(contract.category, model.model_id)
        forecast = model.predict(contract, now=now)
        if (
            forecast.model_id != model.model_id
            or not finite(forecast.at)
            or not 0 <= now - forecast.at <= 5
            or type(forecast.probability) not in (int, float)
            or not math.isfinite(forecast.probability)
            or not 0 <= forecast.probability <= 1
            or type(forecast.uncertainty) not in (int, float)
            or not math.isfinite(forecast.uncertainty)
            or not 0.01 <= forecast.uncertainty < 1
            or not forecast.groups
            or len(set(forecast.groups)) != len(forecast.groups)
            or any(not isinstance(g, str) or not g.strip() for g in forecast.groups)
        ):
            raise ValueError("Invalid/uncalibrated forecast or unknown correlated exposure.")
        choices = []
        for side, p in (("yes", forecast.probability), ("no", 1 - forecast.probability)):
            limit = int((book.ask(side) * 100).to_integral_value(rounding=ROUND_CEILING)) + 2
            if 1 <= limit <= 99:
                risk = limit + fee(limit, contract.fee_multiplier)
                # Cover a possible pre-close sale as well as the entry costs.
                exit_cost = fee(50, contract.fee_multiplier) + 2
                net = (
                    Decimal(str(p))
                    - Decimal(str(forecast.uncertainty))
                    - Decimal(risk) / 100
                    - Decimal(exit_cost) / 100
                )
                if risk <= 100 and net >= Decimal("0.08"):
                    choices.append((float(net), side, limit, risk))
        if not choices:
            raise ValueError("Neither YES nor NO meets conservative 8pp after-cost edge.")
        edge, side, limit, risk = max(choices)
        return Decision(contract.ticker, side, limit, risk, edge, forecast, "")
    except (ValueError, TypeError, KeyError, ArithmeticError, SimulationOnlyError) as exc:
        return Decision(contract.ticker, None, None, 0, None, forecast, str(exc))


class PaperEngine:
    """Single-DB atomic paper intents/fills/cash/settlements with sticky shutdown.

    One whole contract per market lifetime, at most 3 unresolved markets,
    1 per category and 1 per reviewed correlation group. All open risk counts
    against the $2 daily gross-loss budget (even positions carried from yesterday).
    Orders only fill on a different, strictly later valid quote.
    """

    def __init__(self, path: Path, models: Mapping[Category, ProbabilityModel] | None = None):
        require_simulation()
        if path.name == "paper.db" or path.resolve() == Path("data/paper.db").resolve():
            raise ValueError("Use a separate new multi-market trial, never the original ledger.")
        if path.exists():
            prior = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            try:
                saved = prior.execute("SELECT policy FROM mm_manifest WHERE id=1").fetchone()
                if saved is None or saved[0] != canonical(POLICY):
                    raise ValueError("Immutable trial policy mismatch.")
            except sqlite3.Error as exc:
                raise ValueError("Never adopt an unrelated database.") from exc
            finally:
                prior.close()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.models = dict(reviewed_models() if models is None else models)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS mm_manifest(id INTEGER PRIMARY KEY,policy TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mm_control(
                id INTEGER PRIMARY KEY,killed INTEGER,reason TEXT);
            INSERT OR IGNORE INTO mm_control VALUES(1,1,'initial disabled trial');
            CREATE TABLE IF NOT EXISTS mm_account(id INTEGER PRIMARY KEY,cash INTEGER);
            INSERT OR IGNORE INTO mm_account VALUES(1,5000);
            CREATE TABLE IF NOT EXISTS mm_intents(
              ticker TEXT PRIMARY KEY,category TEXT,groups_json TEXT,side TEXT,risk INTEGER,
              contract TEXT,status TEXT,created REAL,realized INTEGER,closed REAL);
            CREATE TABLE IF NOT EXISTS mm_orders(
              id TEXT PRIMARY KEY,ticker TEXT,action TEXT,limit_price INTEGER,
              created REAL,status TEXT,quote_id TEXT);
            CREATE TABLE IF NOT EXISTS mm_fills(
              order_id TEXT PRIMARY KEY,ticker TEXT,action TEXT,price INTEGER,fee INTEGER,at REAL);
            CREATE TABLE IF NOT EXISTS mm_settlements(
              ticker TEXT PRIMARY KEY,outcome INTEGER,payout INTEGER,at REAL,evidence TEXT);
            CREATE TABLE IF NOT EXISTS mm_quotes(
              id TEXT PRIMARY KEY,ticker TEXT,at REAL,payload TEXT);
            CREATE TABLE IF NOT EXISTS mm_events(
              id INTEGER PRIMARY KEY,at REAL,kind TEXT,ticker TEXT,payload TEXT);
            CREATE TABLE IF NOT EXISTS mm_equity(at REAL PRIMARY KEY,value INTEGER);
        """)
        self.db.execute("INSERT OR IGNORE INTO mm_manifest VALUES(1,?)", (canonical(POLICY),))
        # Audit on every open, including restart. Do not silently repair money.
        try:
            self.db.execute("BEGIN")
            self.audit()
            self.db.execute("COMMIT")
        except (ValueError, sqlite3.Error):
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            self.db.execute(
                "UPDATE mm_control SET killed=1,reason='ledger audit failed' WHERE id=1"
            )
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        require_simulation()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.audit()
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def event(self, at: float, kind: str, ticker: str, payload: Any) -> None:
        self.db.execute(
            "INSERT INTO mm_events VALUES(NULL,?,?,?,?)", (at, kind, ticker, canonical(payload))
        )

    def audit(self) -> None:
        cash = self.db.execute("SELECT cash FROM mm_account WHERE id=1").fetchone()[0]
        flows = self.db.execute(
            "SELECT COALESCE(SUM(CASE action WHEN 'buy' THEN -price-fee "
            "ELSE price-fee END),0) FROM mm_fills"
        ).fetchone()[0]
        payouts = self.db.execute("SELECT COALESCE(SUM(payout),0) FROM mm_settlements").fetchone()[
            0
        ]
        if type(cash) is not int or cash < 0 or cash != 5000 + flows + payouts:
            raise ValueError("Paper cash/fee reconciliation failed.")
        orphan = self.db.execute(
            "SELECT 1 FROM mm_fills f LEFT JOIN mm_orders o ON o.id=f.order_id "
            "WHERE o.id IS NULL OR o.status!='filled' "
            "OR o.ticker!=f.ticker OR o.action!=f.action"
        ).fetchone()
        if orphan:
            raise ValueError("Unknown fill/order history.")
        for row in self.db.execute("SELECT * FROM mm_intents"):
            fills = self.db.execute(
                "SELECT * FROM mm_fills WHERE ticker=?", (row["ticker"],)
            ).fetchall()
            buys = [f for f in fills if f["action"] == "buy"]
            sells = [f for f in fills if f["action"] == "sell"]
            settled = self.db.execute(
                "SELECT * FROM mm_settlements WHERE ticker=?", (row["ticker"],)
            ).fetchone()
            if (
                len(buys) > 1
                or len(sells) > 1
                or (sells and not buys)
                or (sells and settled)
                or any(
                    type(f["price"]) is not int
                    or type(f["fee"]) is not int
                    or not 1 <= f["price"] <= 99
                    or f["fee"] < 2
                    for f in fills
                )
                or (buys and buys[0]["price"] + buys[0]["fee"] > row["risk"])
                or (row["status"] == "held" and (len(buys) != 1 or sells or settled))
                or (row["status"] == "pending" and fills)
                or row["status"] not in ("pending", "held", "closed", "cancelled")
                or type(row["risk"]) is not int
                or not 1 <= row["risk"] <= 100
                or (row["status"] == "cancelled" and fills)
                or (row["status"] == "closed" and (len(buys) != 1 or not (sells or settled)))
            ):
                raise ValueError("Unknown/partial/inconsistent paper inventory.")
            if row["status"] == "closed":
                proceeds = sells[0]["price"] - sells[0]["fee"] if sells else settled["payout"]
                if row["realized"] != proceeds - buys[0]["price"] - buys[0]["fee"]:
                    raise ValueError("Closed profit/loss did not reconcile.")
        active = self.db.execute(
            "SELECT * FROM mm_intents WHERE status IN ('pending','held')"
        ).fetchall()
        categories: set[str] = set()
        groups: set[str] = set()
        if len(active) > 3 or sum(r["risk"] for r in active) > 200:
            raise ValueError("Global position/risk invariant failed.")
        if sum(r["risk"] for r in active if r["status"] == "pending") > cash:
            raise ValueError("Reserved cash does not reconcile.")
        for row in active:
            assigned = set(json.loads(row["groups_json"]))
            if row["category"] in categories or not assigned or groups.intersection(assigned):
                raise ValueError("Category/correlated exposure invariant failed.")
            categories.add(row["category"])
            groups.update(assigned)

    def activate(self) -> None:
        require_simulation()
        if not self.models:
            raise SimulationOnlyError(
                "No validated category models; automatic paper trial stays disabled."
            )
        for category, model in self.models.items():
            require_model_release(category, model.model_id)
        with self.transaction():
            control = self.db.execute("SELECT * FROM mm_control").fetchone()
            if control["reason"] != "initial disabled trial":
                raise SimulationOnlyError(
                    "Never automatically clear a persistent stop; review a new trial release."
                )
            self.db.execute(
                "UPDATE mm_control SET killed=0,reason='reviewed paper-only release' WHERE id=1"
            )

    def shutdown(self, *, now: float, reason: str) -> None:
        if not finite(now) or not reason:
            raise ValueError("Invalid emergency stop.")
        with self.transaction():
            self.db.execute("UPDATE mm_control SET killed=1,reason=? WHERE id=1", (reason,))
            self.db.execute("UPDATE mm_orders SET status='cancelled' WHERE status='pending'")
            self.db.execute("UPDATE mm_intents SET status='cancelled' WHERE status='pending'")
            self.event(now, "kill", "", {"reason": reason, "inventory_retained": True})

    def _reserve(self, contract: Contract, decision: Decision, book: Book, now: float) -> bool:
        if (
            decision.reason
            or decision.forecast is None
            or decision.side is None
            or decision.limit is None
        ):
            return False
        if self.db.execute(
            "SELECT 1 FROM mm_intents WHERE ticker=?", (contract.ticker,)
        ).fetchone():
            return False
        active = self.db.execute(
            "SELECT * FROM mm_intents WHERE status IN ('pending','held')"
        ).fetchall()
        groups = set(decision.forecast.groups) | {"event:" + contract.event}
        # Treat risk assets conservatively across crypto/Fed/elections/equities.
        if contract.category in (
            Category.CRYPTO,
            Category.ECONOMICS,
            Category.FINANCIAL,
            Category.POLITICS,
        ):
            groups.add("macro:risk-assets")
        if len(active) >= 3 or any(
            r["category"] == contract.category.value
            or groups.intersection(json.loads(r["groups_json"]))
            for r in active
        ):
            return False
        reserved = sum(r["risk"] for r in active if r["status"] == "pending")
        cash = self.db.execute("SELECT cash FROM mm_account").fetchone()[0]
        day = int(now // 86400) * 86400
        losses = self.db.execute(
            "SELECT COALESCE(SUM(MAX(0,-realized)),0) FROM mm_intents WHERE closed>=? AND closed<?",
            (day, day + 86400),
        ).fetchone()[0]
        if (
            not 1 <= decision.risk <= 100
            or cash - reserved < decision.risk
            or losses + sum(r["risk"] for r in active) + decision.risk > 200
        ):
            return False
        self.db.execute(
            "INSERT INTO mm_intents VALUES(?,?,?,?,?,?,'pending',?,NULL,NULL)",
            (
                contract.ticker,
                contract.category.value,
                canonical(sorted(groups)),
                decision.side,
                decision.risk,
                canonical(asdict(contract)),
                now,
            ),
        )
        self.db.execute(
            "INSERT INTO mm_orders VALUES(?,?, 'buy',?,?,'pending',?)",
            (
                "entry:" + digest(contract.ticker),
                contract.ticker,
                decision.limit,
                now,
                book.quote_id,
            ),
        )
        self.event(now, "entry_intent", contract.ticker, asdict(decision))
        return True

    def _fill(self, contract: Contract, book: Book, decision: Decision, now: float) -> None:
        intent = self.db.execute(
            "SELECT * FROM mm_intents WHERE ticker=?", (contract.ticker,)
        ).fetchone()
        if intent is None:
            return
        if json.loads(intent["contract"])["rules"] != contract.rules:
            raise RuntimeError("Rules changed while exposed.")
        if contract.closes <= now:
            self.db.execute(
                "UPDATE mm_orders SET status='cancelled' WHERE ticker=? AND status='pending'",
                (contract.ticker,),
            )
            if intent["status"] == "pending":
                self.db.execute(
                    "UPDATE mm_intents SET status='cancelled' WHERE ticker=?", (contract.ticker,)
                )
            return
        for order in self.db.execute(
            "SELECT * FROM mm_orders WHERE ticker=? AND status='pending'", (contract.ticker,)
        ).fetchall():
            if book.quote_id == order["quote_id"] or book.event_at <= order["created"]:
                continue
            buy = order["action"] == "buy"
            if buy and (
                now - order["created"] > 5 or decision.reason or decision.side != intent["side"]
            ):
                self.db.execute(
                    "UPDATE mm_orders SET status='cancelled' WHERE id=?", (order["id"],)
                )
                self.db.execute(
                    "UPDATE mm_intents SET status='cancelled' WHERE ticker=?", (contract.ticker,)
                )
                self.event(
                    now,
                    "entry_cancelled",
                    contract.ticker,
                    {"reason": "expired/changed calibrated signal"},
                )
                continue
            price = (
                int((book.ask(intent["side"]) * 100).to_integral_value(rounding=ROUND_CEILING)) + 2
                if buy
                else int((book.bid(intent["side"]) * 100).to_integral_value(rounding=ROUND_FLOOR))
                - 2
            )
            charged = fee(price, contract.fee_multiplier) if 1 <= price <= 99 else 100
            if (
                not 1 <= price <= 99
                or (buy and (price > order["limit_price"] or price + charged > intent["risk"]))
                or (not buy and (price < order["limit_price"] or price < charged))
            ):
                continue
            self.db.execute("UPDATE mm_orders SET status='filled' WHERE id=?", (order["id"],))
            self.db.execute(
                "INSERT INTO mm_fills VALUES(?,?,?,?,?,?)",
                (order["id"], contract.ticker, order["action"], price, charged, now),
            )
            self.db.execute(
                "UPDATE mm_account SET cash=cash+?", (-price - charged if buy else price - charged,)
            )
            if buy:
                self.db.execute(
                    "UPDATE mm_intents SET status='held' WHERE ticker=?", (contract.ticker,)
                )
            else:
                original = self.db.execute(
                    "SELECT price+fee FROM mm_fills WHERE ticker=? AND action='buy'",
                    (contract.ticker,),
                ).fetchone()[0]
                self.db.execute(
                    "UPDATE mm_intents SET status='closed',realized=?,closed=? WHERE ticker=?",
                    (price - charged - original, now, contract.ticker),
                )
            self.event(
                now,
                "fill",
                contract.ticker,
                {"action": order["action"], "price": price, "fee": charged},
            )
        intent = self.db.execute(
            "SELECT * FROM mm_intents WHERE ticker=?", (contract.ticker,)
        ).fetchone()
        # Proposed paper policy only: one pre-close exit, otherwise await settlement.
        if intent["status"] == "held" and 0 < contract.closes - now <= 60:
            price = (
                int((book.bid(intent["side"]) * 100).to_integral_value(rounding=ROUND_FLOOR)) - 2
            )
            if 1 <= price <= 99 and price >= fee(price, contract.fee_multiplier):
                cursor = self.db.execute(
                    "INSERT OR IGNORE INTO mm_orders VALUES(?,?,'sell',?,?,'pending',?)",
                    ("exit:" + digest(contract.ticker), contract.ticker, price, now, book.quote_id),
                )
                if cursor.rowcount:
                    self.event(
                        now, "exit_intent", contract.ticker, {"limit": price, "rule": "last-60s"}
                    )

    def cycle(self, frames: list[tuple[Contract, Book]], *, now: float) -> list[Decision]:
        require_simulation()
        if not finite(now):
            raise ValueError("Invalid cycle time.")
        decisions = []
        try:
            if not self.db.execute("SELECT killed FROM mm_control").fetchone()[0]:
                try:
                    if not self.models:
                        raise SimulationOnlyError("Model registry unavailable on restart.")
                    for category, model in self.models.items():
                        require_model_release(category, model.model_id)
                except SimulationOnlyError:
                    self.shutdown(now=now, reason="model release missing/revoked")
            with self.transaction():
                previous_time = self.db.execute("SELECT MAX(at) FROM mm_events").fetchone()[0]
                if previous_time is not None and now < previous_time:
                    raise RuntimeError("Clock regressed.")
                active = not self.db.execute("SELECT killed FROM mm_control").fetchone()[0]
                candidates = []
                for contract, book in frames:
                    decision = rank(contract, book, self.models.get(contract.category), now=now)
                    decisions.append(decision)
                    self.event(now, "rank", contract.ticker, asdict(decision))
                    try:
                        book.check(now=now)
                        if (
                            contract.ticker != book.ticker
                            or not 0 <= now - contract.metadata_at <= 60
                        ):
                            raise ValueError("Unverified frame.")
                        prior = self.db.execute(
                            "SELECT payload FROM mm_quotes WHERE id=?", (book.quote_id,)
                        ).fetchone()
                        encoded = canonical(asdict(book))
                        if prior:
                            if prior[0] != encoded:
                                raise RuntimeError("Conflicting duplicate quote ID.")
                            continue
                        previous = self.db.execute(
                            "SELECT MAX(at) FROM mm_quotes WHERE ticker=?", (contract.ticker,)
                        ).fetchone()[0]
                        if previous is not None and book.event_at <= previous:
                            raise ValueError("Regressing/duplicated event timestamp.")
                        self.db.execute(
                            "INSERT INTO mm_quotes VALUES(?,?,?,?)",
                            (book.quote_id, contract.ticker, book.event_at, encoded),
                        )
                        if active:
                            self._fill(contract, book, decision, now)
                            if not decision.reason:
                                candidates.append((contract, decision, book))
                    except ValueError as exc:
                        self.event(now, "quote_rejected", contract.ticker, {"reason": str(exc)})
                if active:
                    for contract, decision, book in sorted(
                        candidates, key=lambda c: c[1].edge or -1, reverse=True
                    ):
                        self._reserve(contract, decision, book, now)
                # Never fabricate inventory marks; unknown mark => no equity point.
                holdings = self.db.execute(
                    "SELECT * FROM mm_intents WHERE status='held'"
                ).fetchall()
                marks: dict[str, int] = {}
                for contract, book in frames:
                    try:
                        book.check(now=now)
                        row = next((r for r in holdings if r["ticker"] == contract.ticker), None)
                        if row:
                            price = (
                                int(
                                    (book.bid(row["side"]) * 100).to_integral_value(
                                        rounding=ROUND_FLOOR
                                    )
                                )
                                - 2
                            )
                            if 1 <= price <= 99:
                                marks[contract.ticker] = max(
                                    0, price - fee(price, contract.fee_multiplier)
                                )
                    except ValueError:
                        pass
                if all(r["ticker"] in marks for r in holdings):
                    cash = self.db.execute("SELECT cash FROM mm_account").fetchone()[0]
                    self.db.execute(
                        "INSERT OR IGNORE INTO mm_equity VALUES(?,?)",
                        (now, cash + sum(marks[r["ticker"]] for r in holdings)),
                    )
                self.event(now, "heartbeat", "", {"entries_enabled": active, "frames": len(frames)})
        except (RuntimeError, sqlite3.Error, ValueError):
            # Arm even when money is corrupt and ordinary audited transactions
            # must fail. Do not repair balances or release inventory reservations.
            self.db.execute(
                "UPDATE mm_control SET killed=1,"
                "reason='integrity failure; manual reconciliation required' WHERE id=1"
            )
            raise
        return decisions

    def settle(
        self, contract: Contract, raw: dict[str, Any], *, received_at: float, now: float
    ) -> None:
        require_simulation()
        if (
            not finite(now)
            or not finite(received_at)
            or not 0 <= now - received_at <= 5
            or raw.get("ticker") != contract.ticker
            or raw.get("event_ticker") != contract.event
            or raw.get("status") != "finalized"
            or raw.get("result") not in ("yes", "no")
            or rules_digest(raw, contract.series) != contract.rules
            or utc_timestamp(raw["close_time"]) != contract.closes
            or not contract.closes <= utc_timestamp(raw["settlement_ts"]) <= received_at
        ):
            raise ValueError("Unverified finalized Kalshi outcome; never use a proxy.")
        outcome = int(raw["result"] == "yes")
        if str(raw.get("settlement_value_dollars")) not in (str(outcome), f"{outcome}.0000"):
            raise ValueError("Missing/conflicting official binary settlement value.")
        with self.transaction():
            existing = self.db.execute(
                "SELECT * FROM mm_settlements WHERE ticker=?", (contract.ticker,)
            ).fetchone()
            if existing:
                if existing["outcome"] != outcome:
                    raise ValueError("Conflicting finalized settlement.")
                return
            intent = self.db.execute(
                "SELECT * FROM mm_intents WHERE ticker=?", (contract.ticker,)
            ).fetchone()
            if intent is None or json.loads(intent["contract"])["rules"] != contract.rules:
                raise ValueError("Unknown/changed settlement contract.")
            self.db.execute(
                "UPDATE mm_orders SET status='cancelled' WHERE ticker=? AND status='pending'",
                (contract.ticker,),
            )
            if intent["status"] == "pending":
                self.db.execute(
                    "UPDATE mm_intents SET status='cancelled' WHERE ticker=?", (contract.ticker,)
                )
                return
            if intent["status"] != "held":
                return
            payout = 100 if (intent["side"] == "yes") == bool(outcome) else 0
            original = self.db.execute(
                "SELECT price+fee FROM mm_fills WHERE ticker=? AND action='buy'", (contract.ticker,)
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO mm_settlements VALUES(?,?,?,?,?)",
                (contract.ticker, outcome, payout, now, canonical(raw)),
            )
            self.db.execute("UPDATE mm_account SET cash=cash+?", (payout,))
            self.db.execute(
                "UPDATE mm_intents SET status='closed',realized=?,closed=? WHERE ticker=?",
                (payout - original, now, contract.ticker),
            )
            self.event(
                now, "settlement", contract.ticker, {"payout": payout, "pnl": payout - original}
            )
