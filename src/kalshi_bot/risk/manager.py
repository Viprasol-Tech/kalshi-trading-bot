"""Risk management: pre-trade checks and position sizing.

The :class:`RiskManager` vetoes orders that would breach configured limits and
provides a fractional-Kelly position sizer. Every order produced by a strategy
passes through :meth:`check` before it can be sent.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kalshi_bot.exchange.models import OrderRequest

if TYPE_CHECKING:
    from kalshi_bot.config import RiskSettings


@dataclass(slots=True, frozen=True)
class RiskLimits:
    """Configurable risk limits."""

    max_contracts_per_order: int = 100
    max_position_per_market: int = 500
    max_order_notional_cents: int = 50_000  # max cents committed by one order
    kelly_fraction: float = 0.25  # fractional Kelly (quarter-Kelly default)

    def __post_init__(self) -> None:
        if (
            min(self.max_contracts_per_order, self.max_position_per_market,
                self.max_order_notional_cents) <= 0
            or not 0 < self.kelly_fraction <= 1
        ):
            raise ValueError("Invalid risk limits.")

    @classmethod
    def from_settings(cls, risk: RiskSettings) -> RiskLimits:
        """Build limits from validated :class:`~kalshi_bot.config.RiskSettings`."""
        return cls(
            max_contracts_per_order=risk.max_contracts_per_order,
            max_position_per_market=risk.max_position_per_market,
            max_order_notional_cents=risk.max_order_notional_cents,
            kelly_fraction=risk.kelly_fraction,
        )


@dataclass(slots=True)
class RiskDecision:
    """Result of a pre-trade risk check."""

    approved: bool
    reason: str = ""


class RiskManager:
    """Enforces pre-trade risk limits and sizes positions."""

    def __init__(self, limits: RiskLimits | None = None) -> None:
        self.limits = limits or RiskLimits()

    @classmethod
    def from_settings(cls, risk: RiskSettings) -> RiskManager:
        """Build a risk manager from :class:`~kalshi_bot.config.RiskSettings`."""
        return cls(RiskLimits.from_settings(risk))

    def check(
        self, order: OrderRequest, current_position: int, *,
        available_cash: int | None = None,
        pending_orders: Sequence[OrderRequest] = (),
        estimated_fees: int | None = None,
    ) -> RiskDecision:
        """Approve or reject an order against the configured limits."""
        if order.count > self.limits.max_contracts_per_order:
            return RiskDecision(False, "order count exceeds max_contracts_per_order")

        if available_cash is None or available_cash <= 0:
            return RiskDecision(False, "missing or insufficient available cash")
        # Opposing pending orders cannot be netted: only one side might fill.
        deltas = [o.signed_count for o in pending_orders if o.ticker == order.ticker]
        deltas.append(order.signed_count)
        low = current_position + sum(d for d in deltas if d < 0)
        high = current_position + sum(d for d in deltas if d > 0)
        if max(abs(low), abs(high)) > self.limits.max_position_per_market:
            return RiskDecision(False, "projected position exceeds max_position_per_market")

        price = order.limit_price
        fees = 2 * order.count if estimated_fees is None else estimated_fees
        if fees < 0:
            return RiskDecision(False, "invalid estimated fees")
        notional = price * order.count + fees
        if notional > self.limits.max_order_notional_cents:
            return RiskDecision(False, "order notional exceeds max_order_notional_cents")
        if notional > available_cash:
            return RiskDecision(False, "insufficient available cash including fees")

        return RiskDecision(True)

    def kelly_size(self, edge_prob: float, price_cents: int, bankroll_cents: int) -> int:
        """Fractional-Kelly contract count for a binary contract.

        Args:
            edge_prob: Your estimated probability the contract resolves YES (0-1).
            price_cents: Current YES price in cents (1-99).
            bankroll_cents: Available bankroll in cents.

        Returns:
            Number of contracts to buy (>= 0), capped by per-order limits.

        For a contract priced ``p`` (as a fraction) that pays 1 on win, the
        Kelly fraction is ``f = (edge_prob - p) / (1 - p)``. A non-positive
        result means no edge, so size 0.
        """
        p = price_cents / 100.0
        if (
            not 0.0 < p < 1.0 or not math.isfinite(edge_prob)
            or not 0 <= edge_prob <= 1 or bankroll_cents <= 0
        ):
            return 0
        full_kelly = (edge_prob - p) / (1.0 - p)
        if full_kelly <= 0:
            return 0
        fraction = full_kelly * self.limits.kelly_fraction
        stake_cents = bankroll_cents * fraction
        contracts = int(stake_cents // price_cents)
        return max(0, min(contracts, self.limits.max_contracts_per_order))
