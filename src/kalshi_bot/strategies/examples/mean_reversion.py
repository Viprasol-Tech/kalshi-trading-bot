"""Example: a Bollinger-style mean-reversion strategy.

Tracks a rolling mean and standard deviation of the last price and fades moves
that stretch beyond ``z`` standard deviations: buy YES when price is unusually
cheap, sell when unusually rich, and flatten back toward the mean.
**Educational example only** — not a profitable strategy. See the disclaimer.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import statistics
from collections import deque
from typing import Any

from kalshi_bot.exchange.models import Action, OrderRequest, OrderType, Side
from kalshi_bot.strategies.base import Strategy, StrategyContext


class MeanReversion(Strategy):
    """Fade deviations from a rolling mean of the last price.

    Params:
        window: Number of recent prices for the rolling mean/std.
        z: Number of standard deviations that triggers an entry.
        size: Contracts per entry.
    """

    name = "mean_reversion"

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        window = int(params.get("window", 20))
        self._prices: deque[int] = deque(maxlen=window)

    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        m = ctx.market
        if m.last_price is None or m.yes_bid is None or m.yes_ask is None:
            return []

        self._prices.append(m.last_price)
        if len(self._prices) < self._prices.maxlen:  # type: ignore[operator]
            return []

        mean = statistics.fmean(self._prices)
        std = statistics.pstdev(self._prices)
        if std == 0:
            return []

        z = float(self.params.get("z", 1.5))
        size = int(self.params.get("size", 1))
        inventory = ctx.position_for(m.ticker)
        score = (m.last_price - mean) / std

        # Unusually cheap and flat -> buy YES (expect reversion up).
        if score <= -z and inventory == 0:
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

        # Reverted back to (or above) the mean while long -> take profit / flatten.
        if score >= 0 and inventory > 0:
            return [
                OrderRequest(
                    ticker=m.ticker,
                    action=Action.SELL,
                    side=Side.YES,
                    count=inventory,
                    type=OrderType.LIMIT,
                    yes_price=m.yes_bid,
                )
            ]

        return []
