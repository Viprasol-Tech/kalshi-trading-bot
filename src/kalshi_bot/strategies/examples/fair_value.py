"""Example: a fair-value strategy with Kelly-sized entries.

You supply your own estimate of the true probability a market resolves YES
(``fair_prob``). When the market's YES ask is cheaper than your fair value by a
configurable margin, the strategy buys YES, sizing the position with the
risk manager's fractional-Kelly sizer. **Educational example only** — the fair
value is a fixed parameter here; a real edge requires a real model.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from typing import Any

from kalshi_bot.exchange.models import Action, OrderRequest, OrderType, Side
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.strategies.base import Strategy, StrategyContext


class FairValue(Strategy):
    """Buy YES when it trades below your estimated fair probability.

    Params:
        fair_prob: Your estimated P(resolves YES), 0-1.
        margin_cents: Required gap between fair value (in cents) and the ask.
        max_position: Stop adding once abs(inventory) reaches this.
    """

    name = "fair_value"

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        self._risk = RiskManager()

    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        m = ctx.market
        if m.yes_ask is None:
            return []

        fair_prob = float(self.params.get("fair_prob", 0.5))
        margin = int(self.params.get("margin_cents", 3))
        max_position = int(self.params.get("max_position", 100))
        fair_cents = fair_prob * 100.0

        inventory = ctx.position_for(m.ticker)
        if inventory >= max_position:
            return []

        # Only act when the ask is comfortably below our fair value.
        if m.yes_ask > fair_cents - margin:
            return []

        size = self._risk.kelly_size(
            edge_prob=fair_prob,
            price_cents=m.yes_ask,
            bankroll_cents=max(ctx.balance, m.yes_ask),
        )
        size = max(1, min(size, max_position - inventory))
        return [
            OrderRequest(
                ticker=m.ticker,
                action=Action.BUY,
                side=Side.YES,
                count=size,
                type=OrderType.LIMIT,
                yes_price=m.yes_ask,
            )
        ]
