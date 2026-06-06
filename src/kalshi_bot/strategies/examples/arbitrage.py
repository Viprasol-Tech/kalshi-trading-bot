"""Example: a YES/NO arbitrage strategy.

On Kalshi, a YES contract and its matching NO contract together resolve to
exactly 100 cents. If ``yes_ask + no_ask < 100`` you can buy both sides and lock
in the difference (minus fees) regardless of the outcome. This strategy detects
that condition and buys the cheaper pair.

This is the closest thing to a "free lunch" the framework ships, but such gaps
are rare, small, and fee-sensitive in practice. **Educational example only.**

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from kalshi_bot.exchange.models import Action, OrderRequest, OrderType, Side
from kalshi_bot.strategies.base import Strategy, StrategyContext


class ArbitrageYesNo(Strategy):
    """Buy both YES and NO when their combined ask is below 100 cents.

    Params:
        size: Contracts per side.
        min_edge_cents: Minimum ``100 - (yes_ask + no_ask)`` required to trade,
            a buffer to cover fees and slippage.
    """

    name = "arbitrage"

    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        m = ctx.market
        if m.yes_ask is None or m.no_ask is None:
            return []

        min_edge = int(self.params.get("min_edge_cents", 2))
        edge = 100 - (m.yes_ask + m.no_ask)
        if edge < min_edge:
            return []

        size = int(self.params.get("size", 1))
        return [
            OrderRequest(
                ticker=m.ticker,
                action=Action.BUY,
                side=Side.YES,
                count=size,
                type=OrderType.LIMIT,
                yes_price=m.yes_ask,
            ),
            OrderRequest(
                ticker=m.ticker,
                action=Action.BUY,
                side=Side.NO,
                count=size,
                type=OrderType.LIMIT,
                no_price=m.no_ask,
            ),
        ]
