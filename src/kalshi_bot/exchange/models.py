"""Typed data models for Kalshi API objects.

These cover the subset used by the framework. Prices are integer **cents**
(1-99). Quantities (``count``) are whole contracts.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Side(str, Enum):
    """Contract side."""

    YES = "yes"
    NO = "no"


class Action(str, Enum):
    """Order action."""

    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    """Supported order types."""

    LIMIT = "limit"
    MARKET = "market"


class TimeInForce(str, Enum):
    """Order time-in-force."""

    GOOD_TILL_CANCELED = "good_till_canceled"
    IMMEDIATE_OR_CANCEL = "immediate_or_cancel"
    FILL_OR_KILL = "fill_or_kill"


class Market(BaseModel):
    """A tradable Kalshi market."""

    ticker: str
    event_ticker: str | None = None
    title: str | None = None
    status: str | None = None
    yes_bid: int | None = None
    yes_ask: int | None = None
    no_bid: int | None = None
    no_ask: int | None = None
    last_price: int | None = None
    volume: int | None = None
    open_interest: int | None = None


class OrderRequest(BaseModel):
    """Payload for creating an order (``POST /portfolio/orders``)."""

    ticker: str
    action: Action
    side: Side
    count: int = Field(gt=0, description="Number of contracts.")
    type: OrderType = OrderType.LIMIT
    yes_price: int | None = Field(default=None, ge=1, le=99)
    no_price: int | None = Field(default=None, ge=1, le=99)
    time_in_force: TimeInForce | None = None
    client_order_id: str | None = None
    post_only: bool | None = None

    def to_payload(self) -> dict[str, object]:
        """Serialise to the JSON body Kalshi expects, omitting unset fields."""
        return self.model_dump(exclude_none=True, mode="json")


class Order(BaseModel):
    """An order as returned by Kalshi."""

    order_id: str
    ticker: str
    status: str | None = None
    side: Side | None = None
    action: Action | None = None
    yes_price: int | None = None
    no_price: int | None = None
    count: int | None = None
    remaining_count: int | None = None


class Position(BaseModel):
    """A market position.

    ``position`` is signed: positive means net-long YES contracts, negative
    means net-short (equivalently long NO). ``avg_price`` is the volume-weighted
    average entry price in cents and is used for mark-to-market PnL.
    """

    ticker: str
    position: int = 0
    market_exposure: int = 0
    realized_pnl: int = 0
    avg_price: float = 0.0

    @property
    def is_flat(self) -> bool:
        """True when there is no open position."""
        return self.position == 0

    def unrealized_pnl(self, mark_price: int) -> int:
        """Mark-to-market unrealized PnL in cents at ``mark_price``."""
        return round((mark_price - self.avg_price) * self.position)


class Fill(BaseModel):
    """A trade execution (one order may produce several fills)."""

    ticker: str
    side: Side
    action: Action
    count: int = Field(gt=0)
    price: int = Field(ge=1, le=99, description="Execution price in cents.")
    order_id: str | None = None
    trade_id: str | None = None

    @property
    def notional_cents(self) -> int:
        """Cash value of the fill in cents."""
        return self.price * self.count

    @property
    def signed_count(self) -> int:
        """Position delta: positive for buys, negative for sells."""
        return self.count if self.action is Action.BUY else -self.count


class OrderBookLevel(BaseModel):
    """A single price level in an order book."""

    price: int = Field(ge=1, le=99)
    count: int = Field(ge=0)


class OrderBook(BaseModel):
    """A two-sided order book snapshot for a market.

    Kalshi quotes YES and NO books separately; both are expressed in YES-price
    terms here for convenience (a NO bid at ``p`` is a YES ask at ``100 - p``).
    """

    ticker: str
    yes: list[OrderBookLevel] = Field(default_factory=list)
    no: list[OrderBookLevel] = Field(default_factory=list)

    @property
    def best_yes_bid(self) -> int | None:
        """Highest YES bid price, or None if the YES book is empty."""
        return max((lvl.price for lvl in self.yes), default=None)

    @property
    def best_yes_ask(self) -> int | None:
        """Lowest YES ask, derived from the best NO bid (``100 - no_bid``)."""
        best_no = max((lvl.price for lvl in self.no), default=None)
        return None if best_no is None else 100 - best_no

    @property
    def mid_price(self) -> float | None:
        """Mid-price between best YES bid and ask, or None if either is absent."""
        bid, ask = self.best_yes_bid, self.best_yes_ask
        if bid is None or ask is None:
            return None
        return (bid + ask) / 2.0

    @property
    def spread(self) -> int | None:
        """Bid-ask spread in cents, or None if either side is absent."""
        bid, ask = self.best_yes_bid, self.best_yes_ask
        if bid is None or ask is None:
            return None
        return ask - bid


class Balance(BaseModel):
    """Portfolio cash balance (in cents)."""

    balance: int = 0

    @property
    def balance_dollars(self) -> float:
        """Cash balance converted to dollars."""
        return self.balance / 100.0


class Portfolio(BaseModel):
    """Aggregate view of cash plus open positions."""

    balance_cents: int = 0
    positions: list[Position] = Field(default_factory=list)

    def position_for(self, ticker: str) -> Position | None:
        """Return the position for ``ticker``, or None if flat."""
        for pos in self.positions:
            if pos.ticker == ticker:
                return pos
        return None

    def market_value_cents(self, marks: dict[str, int]) -> int:
        """Total mark-to-market equity in cents given ``{ticker: price}`` marks."""
        equity = self.balance_cents
        for pos in self.positions:
            mark = marks.get(pos.ticker)
            if mark is not None:
                equity += pos.position * mark
        return equity
