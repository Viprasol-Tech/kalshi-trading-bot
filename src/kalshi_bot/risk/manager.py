"""Risk management: pre-trade checks and position sizing.

The :class:`RiskManager` vetoes orders that would breach configured limits and
provides a fractional-Kelly position sizer. Every order produced by a strategy
passes through :meth:`check` before it can be sent.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from kalshi_bot.exchange.models import OrderRequest

if TYPE_CHECKING:
    from kalshi_bot.config import RiskSettings


@dataclass(slots=True)
class RiskLimits:
    """Configurable risk limits."""

    max_contracts_per_order: int = 100
    max_position_per_market: int = 500
    max_order_notional_cents: int = 50_000  # max cents committed by one order
    kelly_fraction: float = 0.25  # fractional Kelly (quarter-Kelly default)

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

    def check(self, order: OrderRequest, current_position: int) -> RiskDecision:
        """Approve or reject an order against the configured limits."""
        if order.count > self.limits.max_contracts_per_order:
            return RiskDecision(False, "order count exceeds max_contracts_per_order")

        signed = order.count if order.action.value == "buy" else -order.count
        projected = current_position + signed
        if abs(projected) > self.limits.max_position_per_market:
            return RiskDecision(False, "projected position exceeds max_position_per_market")

        price = order.yes_price or order.no_price or 0
        notional = price * order.count
        if notional > self.limits.max_order_notional_cents:
            return RiskDecision(False, "order notional exceeds max_order_notional_cents")

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
        if not 0.0 < p < 1.0:
            return 0
        full_kelly = (edge_prob - p) / (1.0 - p)
        if full_kelly <= 0:
            return 0
        fraction = full_kelly * self.limits.kelly_fraction
        stake_cents = bankroll_cents * fraction
        contracts = int(stake_cents // price_cents)
        return max(0, min(contracts, self.limits.max_contracts_per_order))
