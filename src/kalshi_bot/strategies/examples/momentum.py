"""Example: a naive last-price momentum strategy.

Buys YES when the last price rises above a moving reference and exits when it
falls back. **Educational example only** — not a profitable strategy. See the
project disclaimer.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from collections import deque
from typing import Any

from kalshi_bot.exchange.models import Action, OrderRequest, OrderType, Side
from kalshi_bot.strategies.base import Strategy, StrategyContext


class Momentum(Strategy):
    """Trade in the direction of short-term last-price momentum.

    Params:
        window: Number of recent prices used for the moving average.
        size: Contracts per entry.
    """

    name = "momentum"

    def __init__(self, **params: Any) -> None:
        super().__init__(**params)
        window = int(params.get("window", 10))
        self._prices: deque[int] = deque(maxlen=window)

    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        m = ctx.market
        if m.last_price is None or m.yes_ask is None:
            return []

        self._prices.append(m.last_price)
        if len(self._prices) < self._prices.maxlen:  # type: ignore[operator]
            return []

        avg = sum(self._prices) / len(self._prices)
        size = int(self.params.get("size", 1))
        inventory = ctx.position_for(m.ticker)

        # Momentum up and flat -> enter long YES at the ask.
        if m.last_price > avg and inventory == 0:
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

        # Momentum down and long -> exit.
        if m.last_price < avg and inventory > 0 and m.yes_bid is not None:
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
