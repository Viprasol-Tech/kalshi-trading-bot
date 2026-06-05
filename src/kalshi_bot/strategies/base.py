"""Strategy plugin interface.

Subclass :class:`Strategy` and implement :meth:`on_market_data` to emit
:class:`~kalshi_bot.exchange.models.OrderRequest` objects. The engine handles
execution, risk checks and (in dry-run) simulation.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from kalshi_bot.exchange.models import Market, OrderRequest, Position


class StrategyContext:
    """Read-only snapshot passed to a strategy on each tick."""

    def __init__(self, market: Market, positions: dict[str, Position], balance: int) -> None:
        self.market = market
        self.positions = positions
        self.balance = balance

    def position_for(self, ticker: str) -> int:
        """Net contract position for ``ticker`` (0 if none)."""
        pos = self.positions.get(ticker)
        return pos.position if pos else 0


class Strategy(ABC):
    """Base class for all trading strategies."""

    #: Human-readable strategy name (override in subclasses).
    name: str = "unnamed"

    def __init__(self, **params: Any) -> None:
        self.params: dict[str, Any] = params

    @abstractmethod
    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        """Called on each market-data update.

        Return zero or more orders to submit. Return an empty list to do nothing.
        """
        raise NotImplementedError

    def on_start(self) -> None:
        """Optional hook called once when the engine starts."""

    def on_stop(self) -> None:
        """Optional hook called once when the engine stops."""
