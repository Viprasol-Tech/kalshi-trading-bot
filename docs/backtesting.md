# Backtesting

The `Backtester` replays timestamped `Market` snapshots through the same
conservative paper broker as the engine. See [simulation assumptions](simulation-only.md).
Bare last-price snapshots without executable quote metadata cannot fill orders.

```python
from kalshi_bot.backtesting.engine import Backtester
from kalshi_bot.backtesting.data import random_walk_snapshots
from kalshi_bot.strategies.examples.momentum import Momentum

snapshots = random_walk_snapshots(n=200, seed=42)

report = Backtester(starting_balance_cents=5000).run(Momentum(window=5, size=1), snapshots)

print("trades:", report.trades)
print("PnL ($):", report.pnl_dollars)
print(report.summary())  # sharpe, max_drawdown, win_rate, profit_factor, ...
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

- Later-quote execution with depth limits, partial fills, spread and adverse slippage.
- Funded YES/NO holdings, FIFO costs, reservations and liquidation-value equity.
- Realized PnL on closing trades, including entry and exit fees.
- Two-cent estimated fees per filled contract and mandatory risk checks.

## What it does **not** model (yet)

- Maker queue priority: passive orders are conservatively left unfilled.
- Historical BTC settlement replay and current exchange-specific fee schedules.
- Calibrated annualization for 15-minute returns; legacy metrics use 252 periods.

!!! note
    This backtester is a starting point for evaluating logic — not a
    high-fidelity matching engine. Treat results as directional, not predictive.
    See the roadmap for planned fidelity improvements.
