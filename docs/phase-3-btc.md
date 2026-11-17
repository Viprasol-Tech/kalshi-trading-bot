# Phase 3: experimental, simulation-only BTC 15-minute strategy

This extends the existing bot and persistent $50 paper account. It does not
enable real exchange orders, production credentials, authenticated feeds,
`--live`, a background deployment or automatic promotion to trading.

## Verified contract family

Research checked public demo metadata and official terms on November 11, 2026:

- [Demo KXBTC15M series](https://demo-api.kalshi.co/trade-api/v2/series/KXBTC15M)
- [Official contract terms](https://assets.kalshi.com/contract_terms/CRYPTO15M.pdf)
- [Order book endpoint](https://docs.kalshi.com/api-reference/market/get-market-orderbook)
- [CF passthrough access requirements](https://docs.kalshi.com/cfbenchmarks/rest-passthrough.md)
- [Coinbase public ticker](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-ticker)

The supported primary rule compares the simple average of CF Benchmarks' BRTI
over the final 60 seconds before the end of the interval against the corresponding
60-second average before the start. YES means **at least**, including equality.
The secondary rule specifies rounding to two decimal places.

The rule parser verifies exact wording, settlement source, demo contract-terms
URL, binary $1 payout, locked two-decimal `floor_strike`, comparator, open/close
times, 900-second interval, EST/EDT consistency and metadata status. It rejects
unknown variants. Exact rule text is fingerprinted and rechecked before quotes
and final settlement. A changed rule or strike requires manual review.

**`close_time` ends the 15-minute outcome window.** `expiration_time` can be a
week later for administrative reasons and is not the forecast horizon.
Provider-side `status=open` filtering returned a stale record during inspection;
every result is therefore independently checked. Pagination is bounded, detects
cursor loops and refuses a partially discovered universe on errors.

Demo and production metadata are not assumed equivalent. This code never fetches
production Kalshi data. Researching published descriptions does not enable a
production runtime path.

## Public data and freshness

CF's Kalshi passthrough requires authenticated credentials and entitlement.
Those are forbidden here. The reference adapter instead reads the unsigned
Coinbase BTC-USD ticker, explicitly labeled **proxy**, not BRTI.
Coinbase prices never determine a settlement outcome.

Both feeds require HTTP Date within five seconds, response latency at most
two seconds and no redirects. Requests bypass caches. Coinbase's trade timestamp
must also be no more than five seconds old; the reference spread and deviation
from its midpoint are bounded. The model requires at least 120 observations
covering 120 seconds, a recent contiguous history and non-degenerate volatility.
Future/out-of-order/conflicting ticks, gaps and large jumps fail closed.

The order book contains YES and NO **bids**; a NO bid implies a complementary
YES ask. Dollar-string prices and fractional quantities are parsed as decimals.
Bids round down, asks up and depth down to whole contracts. Crossed raw books
are rejected before rounding. At least four displayed whole contracts are
required for one simulated contract under the 25% participation assumption.

The API does not provide a book event timestamp in this response schema.
`observed_at` is a bounded REST-snapshot receipt time, **not** a claimed exchange
update timestamp. HTTP freshness cannot prove queue position or actual fills.
Sub-cent execution is deliberately approximated by conservative integer cents.

## Estimator and mandatory calibration gate

The versioned estimator is an experimental zero-drift local normal-price
diffusion, with volatility estimated from recent Coinbase log returns.
For a final-minute average, its approximate variance scales with `T - 40`
seconds, not terminal-spot variance `T`. It refuses the final 70 seconds,
including the already-started averaging window, and horizons above 840 seconds.
It is not an exact arithmetic-average or cross-exchange-basis model.

**No validated calibration artifact is included. Default behavior is zero
entries.** Passing deterministic tests proves software behavior, not forecasting
accuracy, calibration or positive expected value.

An optional artifact must match the model, feed and verified rule family, declare
observed-demo provenance and include raw pre-close feature histories plus later
confirmed Kalshi labels. The code recomputes probabilities and scores rather
than accepting reported metrics. It rejects duplicate markets, invalid label
references, training/forecast/result ordering violations, stale evaluation,
inadequate coverage and non-matching metadata.

Current deliberately conservative gates:

- At least 200 distinct held-out markets; evaluation no older than seven days.
- At least 30 observations in the current probability and 180-second horizon bucket.
- Brier score better than a constant empirical-prevalence baseline.
- Five-bin expected calibration error at most 0.05.
- Maximum overall-bin and matching local calibration errors at most 0.10.

These engineering thresholds are not a guarantee of statistical significance.
Provenance declarations and URLs alone do not prove source integrity: Phase 4
must audit raw records, confirmed outcomes and the predeclared evaluation split.
The example synthetic histories exist only inside unit tests and are never
exported or supplied to the actual paper command.

## Entry, loss and settlement rules

For each side:

`net_edge = estimated_probability - calibration_haircut - (ask + slippage + fee)/100`

The haircut is at least five probability points and at least the measured
overall/local calibration errors. Ask pricing already includes the spread.
Slippage is one cent and the fee reserve is two cents per contract, inherited
from Phase 2. These are explicit simulated assumptions, not an exchange fee
schedule or an empirically verified fill model.

Entry requires **net edge >= 0.08** after those deductions, adequate displayed
liquidity and unreserved cash. Quantity is capped by **100 cents maximum total
entry loss including fees**. Only funded YES/NO purchases are submitted to the
paper ledger. One durable intent per market prevents duplicate orders and
re-entry after cancellation, rejection or restart.

Submission only reserves cash. A later eligible quote may fill partially or
not at all. Phase 4 adds a minimum one-second simulated fill latency.
Pending orders are rechecked against their actual limits before
fills; stale data, a missing/failed calibration gate, changed side or lost edge
cancels them. Polling ends with pending orders cancelled.

Confirmed `finalized` demo market results alone produce binary settlement.
Rules and strike must still match the verified contract. Ambiguous, changed or
non-binary results do not settle automatically. Ledger payout is idempotent,
and forecast labeling recovers if interrupted after payout.

## Persistence, metrics and command

BTC tables in the existing SQLite ledger retain reference history, verified
market fingerprints, durable entry intents, sampled equity and forecasting
features. The first forecast per market/horizon bucket is retained even if no
order is allowed, preventing entry-only evaluation and preserving raw features
after the rolling reference window expires. Confirmed outcomes label these
records. Every signal/skip is audited; fills, fees and settlements use the
existing paper-broker audit trail. Database files are not committed.

Reports include account cash, reservations, realized P&L and fees; entry attempts;
filled settled-market win rate; liquidation P&L/drawdown; and observed Brier
scores/sample counts. Missing samples are `null`, not a claimed zero error.
Account P&L is account-wide, while BTC outcome metrics are BTC-only.
Missing/error marks value inventory at zero; drawdown can therefore include
conservative missing-mark effects, not realized trading losses.

```bash
# Bounded diagnostic: no artifact means no entries.
KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true KALSHI_API_KEY_ID= \
  .venv/bin/kalshi-bot btc-paper --ticks 1
```

For observation collection, use bounded `--ticks` (maximum 1800) and
`--interval` between 1 and 5 seconds; default interval is two seconds. This is
not a persistent service. A bounded stop cancels unfilled entries and does not
retry that market's logical trade. Filled inventory remains until a later
invocation sees a confirmed result. Do not reset/refund the ledger between runs.

Only after independently audited calibration exists can the optional
`--calibration` path be supplied; every signal still has to pass the gate.
Malformed/oversized files fail before the runner starts. There is no option
to force an uncalibrated order or enable live trading.

## Validation and remaining Phase 4 work

Local validation on Python 3.13.11:

- **163 tests passed**, with sockets forbidden in the test suite.
- Ruff lint/format and strict Mypy passed.
- Regression tests cover rules, actual-vs-administrative expiry, fixed-point
  books, stale inputs, calibration, net edge, YES/NO direction, one-trade budgets,
  persistence, later-quote fills, cancellation and confirmed-only settlement.
- One bounded actual public-data observation successfully read the demo series,
  discovered/refreshed an active BTC contract, fetched its book and read the
  Coinbase proxy. It skipped the stale demo record and stayed in warm-up.
  Cash stayed **$50**, reserved cash/fees/entry attempts were **zero**.

This is not a historical BTC replay or an empirical calibration result.
No calibration artifact was created from that observation. Phase 4 still needs
timestamp-aligned historical books and proxy/index data, independently verified
labels, fee-schedule/latency/depth checks and an out-of-time, sample-size-aware
performance report. Do not interpret a passing gate implementation as proof
that a real dataset will pass it. No live or production enabling is planned.

See the [Phase 4 data audit and replay report](phase-4-validation.md) for the
subsequent implementation and evidence. Historical execution/calibration remains
blocked; no artifact has been promoted into this runner.
