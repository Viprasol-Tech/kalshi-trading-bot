# Backtesting

The `Backtester` replays a list of `Market` snapshots through a strategy,
simulating immediate fills at each order's limit price, and reports metrics.

```python
from kalshi_bot.backtesting.engine import Backtester
from kalshi_bot.exchange.models import Market
from kalshi_bot.strategies.examples.momentum import Momentum

snapshots = [
    Market(ticker="TEST", last_price=p, yes_bid=p - 1, yes_ask=p + 1)
    for p in [40, 42, 45, 48, 55, 60, 58, 50, 45]
]

result = Backtester(starting_balance_cents=100_000).run(
    Momentum(window=5, size=1), snapshots
)

print("trades:", result.trades)
print("PnL ($):", result.realized_pnl_dollars)
print("final position:", result.final_position)
```

## What it models

- Fills at the order's limit price, immediately (no queue / slippage model).
- Position and cash tracked per snapshot; equity marked to the last price.

## What it does **not** model (yet)

- Order-book depth, partial fills, or maker/taker queue priority.
- Fees and settlement timing.

!!! note
    This backtester is a starting point for evaluating logic — not a
    high-fidelity matching engine. Treat results as directional, not predictive.
    See the roadmap for planned fidelity improvements.
