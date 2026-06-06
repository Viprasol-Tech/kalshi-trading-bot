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

report = Backtester(starting_balance_cents=100_000).run(
    Momentum(window=5, size=1), snapshots
)

print("trades:", report.trades)
print("PnL ($):", report.pnl_dollars)
print(report.summary())   # sharpe, max_drawdown, win_rate, profit_factor, ...
```

Prefer the CLI? Run any bundled strategy on offline synthetic data:

```bash
kalshi-bot backtest momentum --ticks 200 --seed 42
```

## Metrics

`BacktestReport` exposes a full set of quant metrics, all computed from the
equity curve and per-trade PnL log:

| Metric | Property |
|---|---|
| Net PnL (cents / dollars) | `pnl_cents` / `pnl_dollars` |
| Total return | `total_return` |
| Annualised Sharpe ratio | `sharpe` |
| Maximum drawdown | `max_drawdown` |
| Annualised volatility | `volatility` |
| Win rate | `win_rate` |
| Profit factor | `profit_factor` |

The underlying functions live in `kalshi_bot.backtesting.metrics` and are pure
and independently testable. `report.summary()` returns them as a flat dict.

## What it models

- Fills at the order's limit price, immediately (no queue / slippage model).
- Position and cost-basis (VWAP) tracked per snapshot; equity marked to last price.
- Per-trade realised PnL on closing/reducing/flipping trades.
- Optional flat per-contract fees and optional pre-trade `RiskManager` screening.

## What it does **not** model (yet)

- Order-book depth, partial fills, or maker/taker queue priority.
- Settlement timing and percentage-based exchange fees.

!!! note
    This backtester is a starting point for evaluating logic — not a
    high-fidelity matching engine. Treat results as directional, not predictive.
    See the roadmap for planned fidelity improvements.
