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
    """A market position."""

    ticker: str
    position: int = 0
    market_exposure: int = 0
    realized_pnl: int = 0


class Balance(BaseModel):
    """Portfolio cash balance (in cents)."""

    balance: int = 0
