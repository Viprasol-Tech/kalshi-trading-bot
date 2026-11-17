# Phase 1 and Phase 2 validation

Scope: the existing Viprasol-Tech/kalshi-trading-bot repository, based on
`c18882736e780967b22c806f44235bc64163d5f6`. No new app was created.
Phases 3 and 4 were intentionally not started.

## Safety during validation

All execution used `KALSHI_ENVIRONMENT=demo`, `KALSHI_DRY_RUN=true`, and an empty
`KALSHI_API_KEY_ID`. No production credentials were loaded, and no real orders
were submitted. Tests block external socket connections; REST behavior was
tested through mocked transports. No actual market-data requests were needed.

## Checks completed

- Python 3.13.11; dependencies installed from the existing `.[dev]` package.
- Full suite: **116 passed**, with resource warnings promoted to errors.
- Overall test coverage: **90%**; paper broker: **96%**.
- `ruff check .`: passed.
- `ruff format --check .`: passed.
- `mypy src`: passed, 31 source files.
- `git diff --check`: passed.
- All five bundled strategies completed the updated offline example.

The original suite had 56 tests. Tests whose old assumptions depended on instant
fills, a $1,000 bankroll or one-cent fees were updated to assert the new
later-quote execution, $50 bankroll and two-cent estimated fees. Regression
coverage includes all four action/side directions, pending exposure, reserved
cash/inventory, direct HTTP bypass attempts, credential/production/live rejection,
zero cash, duplicate API paths, malformed orders, partial and unfilled orders,
quote freshness/replay, shared complementary liquidity, persistence, competing
ledger connections, settlement idempotence/conflicts and transaction rollback.

## Updated offline momentum run: 200 synthetic ticks, seed 42

| Metric | Result |
|---|---:|
| Initial simulated bankroll | $50.00 |
| Fill events | 10 |
| Contracts filled across entries/exits | 11 |
| Closing-fill sample size | 4 |
| Net liquidation-value P&L | -$0.48 |
| Realized P&L | -$0.38 |
| Estimated fees paid | $0.22 |
| Total return | -0.96% |
| Maximum drawdown | 0.96% |
| Closing-fill win rate | 0% |
| Final net YES inventory | 1 |

The remaining position is open, not assumed settled. The $0.48 loss includes
conservative liquidation marking; it is not entirely realized P&L.

This is synthetic logic validation, **not a historical BTC backtest**, not a
calibration study, and not evidence of an exploitable edge. The original
instant-fill/zero-fee 200-tick run used a $1,000 bankroll and reported a $0.40 loss;
it is not directly comparable to this changed execution model and bankroll.

## Persistent account

`paper-status` created/reopened the separate persistent paper ledger and verified:
cash $50.00; available cash $50.00; reserved cash $0.00; zero fills, positions,
settlements and realized P&L. Offline backtests never debit that account.

See [the simulation guide](simulation-only.md) for explicit fee, depth, slippage,
settlement and reporting assumptions.

## Remaining before a BTC strategy

No BTC discovery, verified settlement-rule adapter, fresh BTC reference feed,
calibrated probability model, eight-point net-edge filter or historical BTC
performance result is implemented by this patch. Public demo snapshots without
executable quote metadata fail closed, so the polling engine will skip them.
Review these Phase 1/2 results before authorizing Phase 3.
