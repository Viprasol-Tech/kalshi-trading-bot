# Strategies

A strategy is a subclass of `kalshi_bot.strategies.base.Strategy` that implements
`on_market_data`, returning a list of `OrderRequest` objects.

## The interface

```python
from kalshi_bot.strategies.base import Strategy, StrategyContext
from kalshi_bot.exchange.models import Action, OrderRequest, OrderType, Side


class BuyCheapYes(Strategy):
    name = "buy_cheap_yes"

    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        m = ctx.market
        if m.yes_ask is not None and m.yes_ask < 20 and ctx.position_for(m.ticker) == 0:
            return [OrderRequest(
                ticker=m.ticker, action=Action.BUY, side=Side.YES,
                count=1, type=OrderType.LIMIT, yes_price=m.yes_ask,
            )]
        return []
```

`StrategyContext` exposes:

- `ctx.market` — the latest `Market` snapshot (prices in cents).
- `ctx.positions` — current positions by ticker.
- `ctx.balance` — cash balance in cents.
- `ctx.position_for(ticker)` — net contracts for a market.

Optional lifecycle hooks: `on_start()` and `on_stop()`.

## Bundled examples

| Strategy | Module | Idea |
|---|---|---|
| `market_maker` | `strategies/examples/market_maker.py` | Quote both sides around mid with an inventory cap. |
| `momentum` | `strategies/examples/momentum.py` | Trade short-term last-price momentum vs. a moving average. |
| `mean_reversion` | `strategies/examples/mean_reversion.py` | Fade moves beyond N standard deviations from a rolling mean. |
| `arbitrage` | `strategies/examples/arbitrage.py` | Buy YES + NO when their combined ask is below 100c. |
| `fair_value` | `strategies/examples/fair_value.py` | Buy YES below your fair probability, Kelly-sized. |

!!! warning
    The bundled strategies are **educational** and not expected to be profitable.

## Risk integration

Every order a strategy returns is passed through the
[`RiskManager`](https://github.com/Viprasol-Tech/kalshi-trading-bot/blob/main/src/kalshi_bot/risk/manager.py)
before it can be submitted. Orders that breach limits are vetoed and logged.

## Running

The five bundled strategies are already registered in `kalshi_bot.cli.STRATEGIES`
(`market_maker`, `momentum`, `mean_reversion`, `arbitrage`, `fair_value`). To add
your own, register it there, then:

```bash
kalshi-bot strategies                                   # list registered strategies
kalshi-bot backtest mean_reversion --ticks 200          # offline metrics
kalshi-bot run buy_cheap_yes <TICKER> --ticks 10        # dry-run
kalshi-bot run buy_cheap_yes <TICKER> --live            # real orders
```
