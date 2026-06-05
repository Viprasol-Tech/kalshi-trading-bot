"""Example: a simple symmetric market-making strategy.

Quotes a YES bid and ask around the current mid-price with a configurable
spread, subject to an inventory cap. **Educational example only** — not a
profitable strategy. See the project disclaimer.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from kalshi_bot.exchange.models import Action, OrderRequest, OrderType, Side
from kalshi_bot.strategies.base import Strategy, StrategyContext


class MarketMaker(Strategy):
    """Quote both sides of a market around its mid-price.

    Params:
        spread_cents: Half-spread distance from mid for each quote.
        quote_size: Contracts per quote.
        max_inventory: Stop quoting the side that would grow abs(inventory)
            beyond this.
    """

    name = "market_maker"

    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        m = ctx.market
        if m.yes_bid is None or m.yes_ask is None:
            return []

        spread = int(self.params.get("spread_cents", 2))
        size = int(self.params.get("quote_size", 1))
        max_inventory = int(self.params.get("max_inventory", 50))

        mid = (m.yes_bid + m.yes_ask) / 2.0
        bid_price = max(1, int(mid - spread))
        ask_price = min(99, int(mid + spread))

        inventory = ctx.position_for(m.ticker)
        orders: list[OrderRequest] = []

        # Buy YES (add to inventory) unless we are already too long.
        if inventory < max_inventory:
            orders.append(
                OrderRequest(
                    ticker=m.ticker,
                    action=Action.BUY,
                    side=Side.YES,
                    count=size,
                    type=OrderType.LIMIT,
                    yes_price=bid_price,
                    post_only=True,
                )
            )

        # Sell YES (reduce inventory) unless we are already too short.
        if inventory > -max_inventory:
            orders.append(
                OrderRequest(
                    ticker=m.ticker,
                    action=Action.SELL,
                    side=Side.YES,
                    count=size,
                    type=OrderType.LIMIT,
                    yes_price=ask_price,
                    post_only=True,
                )
            )

        return orders
