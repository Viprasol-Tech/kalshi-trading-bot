# Simulation-only branch: Phases 1–3

This is a patch to the existing Kalshi Trading Bot, **not a new application**.
The exchange is never used for order submission, cancellation, balances, or
positions. `--live`, production configuration, and configured exchange credentials
are rejected. Public unsigned **demo** market/series GET requests are the only
permitted Kalshi network operations. The default remains `KALSHI_ENVIRONMENT=demo` and
`KALSHI_DRY_RUN=true`; overriding them fails closed rather than enabling trading.
No credentials are required or loaded. Authenticated WebSockets are disabled.

## Safety corrections

- Position direction uses both action and contract side. Buying NO decreases
  net-YES position; selling NO increases it.
- Limit orders require exactly one side-matching price. Unpriced market orders
  and unchecked malformed order models cannot enter the paper broker.
- Contract limits include outstanding orders, considering the worst case where
  only one direction fills. Opposing pending orders cannot cancel each other.
- Missing/zero available cash is rejected. Estimated fees are included in funding
  checks, and cash/inventory reservations are made atomically.
- Both client methods and the final HTTP request hook block real mutations.
- REST URL joining includes `/trade-api/v2` exactly once.
- Fair-value sizing no longer forces a one-contract order when cash/size is zero.

## Persistent paper account

`PaperBroker()` creates or reopens `data/paper.db`, with an initial **5,000 cents**.
It never re-funds an existing account. SQLite transactions, a write lock, and
constraints prevent competing connections from double-spending reserved cash.
Opening a ledger with a different initial bankroll is rejected.

The ledger stores cash, cash reservations, orders and remaining quantities,
separate funded YES/NO lots, FIFO entry costs, fills, fees, confirmed settlements,
realized P&L, accepted quote identities and audit events. Partial fills and pending
orders survive restarts. Settlement is idempotent; conflicting outcomes fail.
Ledger files and SQLite WAL/SHM files are git-ignored.

Each purchase is limited to **100 cents worst-case loss including estimated
entry fees**. Naked shorts are forbidden. Sales require owned, unreserved
contracts and must cover their own exit fees. This is a per-entry-order loss cap,
not a portfolio-wide $1 loss cap. Multiple entries can lose more than $1 in total.
The BTC runner permits only one entry attempt per market, across restarts,
including rejected/cancelled attempts. It cannot split or re-enter a logical
trade to evade the cap. It buys funded contracts only and holds fills to a
confirmed binary settlement; it does not assume an exit sale.

### Explicit conservative execution assumptions

- Estimated fee: **2 cents per filled contract on both entry and exit**. This is
  an assumption, not a verified current Kalshi fee schedule.
- Adverse slippage: **1 cent** above executable asks for purchases, or below bids
  for sales. Never fill outside the order's limit price.
- Participation: at most **25% of displayed opposing depth**, rounded down,
  shared across pending orders and complementary YES/NO liquidity.
- Submission reserves funds but never fills. A fill requires a **later**
  quote with valid timestamps, both bid and ask, sufficient depth and an
  executable price after slippage.
- Quotes older than **5 seconds**, future quotes, repeated identities and
  non-increasing market timestamps cannot trigger fills. The engine also skips
  strategy evaluation on invalid or missing quote metadata.
- Passive/post-only orders receive **no assumed fills**: queue priority is unknown.
  Fill-or-kill is unsupported and rejected. Immediate-or-cancel is evaluated
  against the first later eligible quote, then its unfilled remainder is cancelled.
  This delayed paper IOC rule is not exchange-exact execution.
- Unknown results are never inferred from last price or expiry. Only explicitly
  confirmed binary settlement (0 or 100 cents), with an evidence reference, is
  accepted. Non-binary settlement and corrections require manual review.
- Equity is cash plus liquidation value at bid less exit slippage/fees. Missing
  marks value inventory at zero. Marks must be supplied by the caller from its
  latest accepted quotes; stale-mark portfolio reporting is not a BTC data feed.

## Commands

```bash
# Use a local virtual environment; no exchange credentials.
uv venv .venv
uv pip install --python .venv/bin/python -e ".[dev]"

KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true KALSHI_API_KEY_ID= \
  .venv/bin/kalshi-bot backtest momentum --ticks 200

KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true KALSHI_API_KEY_ID= \
  .venv/bin/kalshi-bot paper-status

KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true KALSHI_API_KEY_ID= \
  .venv/bin/python -m pytest
```

The momentum backtest uses an **isolated in-memory** $50 ledger, synthetic quotes,
and synthetic depth. It does not alter `data/paper.db`. Its P&L is not evidence of
historical BTC performance. Its legacy annualized metrics retain a 252-period
convention and must not be interpreted as calibrated 15-minute annual metrics.
Total return and drawdown now include initial capital, and realized P&L excludes
open inventory. Win rate is over closing fills, not a sample of settled BTC markets.

The polling engine uses the persistent ledger, not an exchange portfolio.
Public market snapshots without timestamps/depth cannot produce paper entries;
the Phase 3 adapter supplies validated fixed-point order books and bounded
observation timestamps. Pending paper orders for the engine's market are
cancelled on stop or exceptions. See [BTC paper strategy](phase-3-btc.md)
for its separate command and stricter gates.

## Phase 3 status and deferred validation

Phase 3 adds verified BTC 15-minute discovery, public Coinbase proxy prices,
executable demo order books, an experimental testable estimator and a mandatory
empirical calibration gate before the eight-point net-edge filter. No validated
artifact is bundled: the default is observation/logging with zero entries.
Coinbase prices are never represented as the CF settlement index.

Phase 4: historical BTC replay, fee-schedule verification and empirical fill
assumptions, calibration evaluation and a sample-size-aware paper performance
report. No historical data was supplied or fetched in Phases 1–3; no historical
BTC backtest or profitability claim has been made.
