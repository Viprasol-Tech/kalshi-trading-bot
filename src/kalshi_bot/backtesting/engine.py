"""A minimal vectorised-ish backtester for prediction-market strategies.

Replays a sequence of :class:`~kalshi_bot.exchange.models.Market` snapshots
through a strategy, simulating immediate fills at the quoted price, and reports
summary metrics. This is intentionally simple — a starting point, not a
high-fidelity matching engine.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_bot.exchange.models import Market, Position
from kalshi_bot.strategies.base import Strategy, StrategyContext


@dataclass(slots=True)
class BacktestResult:
    """Summary of a backtest run."""

    trades: int = 0
    realized_pnl_cents: int = 0
    final_position: int = 0
    equity_curve: list[int] = field(default_factory=list)

    @property
    def realized_pnl_dollars(self) -> float:
        """Realized PnL converted to dollars."""
        return self.realized_pnl_cents / 100.0


class Backtester:
    """Replay market snapshots through a strategy with naive fills."""

    def __init__(self, starting_balance_cents: int = 100_000) -> None:
        self.starting_balance_cents = starting_balance_cents

    def run(self, strategy: Strategy, snapshots: list[Market]) -> BacktestResult:
        """Run ``strategy`` over ``snapshots`` and return metrics.

        Fills assume each order trades instantly at its limit price. Position is
        tracked in YES contracts; PnL is marked against the last seen price.
        """
        result = BacktestResult()
        position = 0
        cost_basis_cents = 0  # signed cents paid for current inventory
        balance = self.starting_balance_cents

        strategy.on_start()
        for market in snapshots:
            ctx = StrategyContext(
                market=market,
                positions={market.ticker: Position(ticker=market.ticker, position=position)},
                balance=balance,
            )
            for order in strategy.on_market_data(ctx):
                price = order.yes_price or order.no_price or 0
                qty = order.count if order.action.value == "buy" else -order.count
                position += qty
                cost_basis_cents += qty * price
                balance -= qty * price
                result.trades += 1
            mark = market.last_price or market.yes_bid or 0
            equity = balance + position * mark
            result.equity_curve.append(equity)
        strategy.on_stop()

        final_equity = result.equity_curve[-1] if result.equity_curve else balance
        result.final_position = position
        result.realized_pnl_cents = final_equity - self.starting_balance_cents
        return result
