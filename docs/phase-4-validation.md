# Phase 4 validation report — historical performance remains blocked

**Status: tools implemented and tested; empirical historical trading validation
is not complete.** No dataset with adequate executable history or independently
audited calibration was available. No calibration artifact was created or enabled.
The existing repository, demo-only boundary, $50 persistent paper account and
$1 per-trade maximum loss remain intact.

## Historical data actually checked

The bounded public-data audit ran on **November 11, 2026 at 15:35 UTC**.
It queried unsigned demo series metadata, the historical cutoff, settled
markets and the older historical partition. It retained a provider-returned
sample of 200 markets; this is neither the full universe nor an unbiased sample.
An additional archived-market probe was retrieved but not added to that sample.

| Check | Observed result |
| --- | --- |
| Sampled settled-market records | 200 |
| Records passing exact rules, strike, payout and settlement-time checks | 199 |
| Rejected records | 1, missing `floor_strike` |
| Confirmed YES / NO labels | 95 / 104 |
| Accepted-label close-time range | Nov 9, 13:45 UTC – Nov 11, 15:30 UTC |
| Kalshi candle probes | 3 markets, 15 one-minute candles each |
| Coinbase closed one-minute candles | 48 |
| Eligible historical execution/forecast sample | **0** |

The rejected ticker was `KXBTC15M-26NOV102230-30`. Its unknown strike was not
inferred from a subtitle, adjacent market or hindsight prices.

The archive cutoff was September 12, 2026. Older archived data and recent
settled data were explicitly distinguished; production and demo records were
not mixed. The retained sample's labels all came from the recent demo partition.

The three Kalshi candle probes ended at 15:00, 15:15 and 15:30 UTC on November 11.
Coinbase candle coverage was restricted to fully closed buckets in the requested
14:42–15:30 UTC window. An OHLC close belongs at the **end** of its bucket, not
the start. Open/incomplete buckets were not counted as historical close prices.

## Why these prices cannot produce an honest trading backtest

Kalshi candles provide bid/ask OHLC and traded volume, not timestamped resting
depth or the within-minute ordering of bid/ask changes. Volume is not available
liquidity. Bar highs/lows cannot prove an order would have filled, and independent
OHLC extremes cannot be combined into an executable spread.

Coinbase minute closes do not satisfy the estimator's at-least-120-observation,
at-most-five-second-gap history requirement. They are not CF Benchmarks' BRTI
and cannot reconstruct its exact final-minute settlement average. No interpolation
or duplicated prices were used to manufacture a higher-frequency history.

Finalized metadata supplies outcome labels, but does not establish which metadata
or strike revision was available to a trader at an earlier decision time.
The checked public historical API catalog exposes markets, candles and trades;
it did not supply a suitable historical resting-depth snapshot archive.
This does not establish that no third-party archive exists.

### Third-party availability check

The search also covered public third-party archives, not just Kalshi's API.
KalshiBackTest advertises approximately 100 ms book snapshots with an underlying
coin price; its documented free tier requires a provider key and limits access
to 50 markets per deployment. Those documentation claims do not verify demo
provenance, feed identity or completeness, and no account/key was obtained.

DepthFeed exposes an account-free four-market sample. Direct HTTPS reads
succeeded: the 1,020-row book file includes **637 snapshots of one BTC market**,
`KXBTC15M-26OCT271200-00`. It contains receipt timestamps and YES/NO price/size
arrays, but **no matching underlying reference ticks**. Its one BTC metadata row
contains a target-price subtitle and outcome, not the required `floor_strike`,
rule text, settlement confirmation time or metadata receipt history. A subtitle
was not promoted into a verified as-of strike. The sample does not establish
demo-environment provenance and was not imported or mixed with the demo audit.

This is evidence that historical depth can be sourced, not that a sufficient
dataset was obtained here. One market without the required reference/provenance
cannot support the strategy's calibration gate or the requested performance
validation. No provider subscription, paid download or credentialed request was
made. The data gap is a **matching, validated dataset**, not a claim that no
historical order books exist anywhere.

Consequently:

| Requested historical metric | Result |
| --- | --- |
| Settled simulated trades | 0 eligible historical executions |
| Win rate | Not estimable |
| Net P&L after fees | Not estimable — **not a claimed $0 strategy result** |
| Maximum trading drawdown | Not estimable |
| Brier score / calibration error | Not estimable |
| Forecast/outcome calibration sample | 0 |

The 199 labels are **not** 199 model forecasts or simulated trades. More labels
alone would not repair missing quotes, reference histories or as-of metadata.

## What was implemented

- `btc-history-audit`: bounded read-only audit of recent/historical demo labels
  and representative candles. Missing metrics are JSON `null`, not invented zeroes.
- `btc-paper --record`: append normalized, timestamped public observations to
  JSONL. Reference observations are recorded even when a quote is unusable.
  Recorded skips preserve cancellations across missing/invalid quote periods.
- `btc-replay`: offline observation-order replay through the existing paper
  broker and strategy. It creates one isolated $50 account for the whole replay,
  never refills it per market and never changes the persistent paper account.
- Reports include cash/reservations, fees, realized and liquidation P&L, drawdown,
  filled settled-market win rate, sample counts, Brier score and five-bin ECE.
  Filled-market win rate also has a Wilson 95% interval. That interval assumes
  independent Bernoulli trials; serially related BTC markets can have greater
  uncertainty. Small test-fixture samples are never profitability evidence.

Replay rejects future/stale receipts, mismatched rules, invalid payout labels,
out-of-order files and conflicting outcomes. Outcomes become visible only at
their observed confirmation time, never merely at close. Calibration evaluated
after a replay decision cannot be used for that earlier decision.

Execution retains the existing spread, one-cent adverse slippage, two-cent
per-contract fee reserve, 25% displayed-depth participation, later-quote fills,
partial/unfilled behavior, cash reservations, one-intent-per-market budget and
eight-point net-edge gate. A **minimum one-second simulated fill latency** was
added. Pending-limit edge comparisons now use decimal arithmetic consistently
at the inclusive eight-point boundary.

Replay has no network client. Candle files and outcome-only files are rejected
as executable inputs. The CLI emits a blocked report and nonzero exit status
instead of presenting a fake performance table. Input is bounded to 64 MB,
20,000 events and 200 KB per line. Larger datasets need a reviewed streaming or
segmentation design before use; silently resetting bankrolls across chunks
would not be a valid whole-period backtest.

Normalized receipt times and declared provenance are not cryptographic proof
of the capture process. A dataset hash ties a report to its input, but independent
source-integrity review is still required. No automatic calibration export,
gate relaxation, promotion or live-trading switch was added.

## Actual public-data smoke check

Two three-cycle observation sessions were run with no calibration artifact.
Most sampled quotes failed the existing cent-resolution/depth requirements.
One valid normalized pre-close snapshot was retained, along with one previously
known market's confirmed outcome. These two records concern two tickers; only
**one** ticker has a replayable pre-close snapshot. The final recorder also
supports independent references/skips for subsequent collection.

The tiny archive replayed offline. Its single reference observation correctly
failed model warm-up. There were **zero entry attempts and zero fills**.
The archive fingerprint was:

`4084c3fca9d08e861a6c4c0219abe144593d9d17ed7262687d610a2a3ade2d00`

This verifies plumbing and no-entry behavior, **not historical forecasting or
trading performance**. The persistent account stayed at **$50 cash, $0 reserved,
$0 realized P&L and $0 fees**. A zero-position settlement recorded no payout
or account gain. The separate smoke replay's operational P&L/drawdown were zero
because it made no trades; those are not the historical metrics requested above.

## Fee evidence and limits

The official fee PDF retrieved for this review states an effective date of
July 7, 2026 and a general taker formula of
`M × 0.07 × C × P × (1-P)`. The demo series metadata reports multiplier one.
Before rounding, that formula peaks at **1.75 cents per contract**.
The retained two-cent-per-contract integer reserve is conservative for this
formula; it is not an exact reconstruction of sub-cent charges, historical
schedule changes, third-party charges or empirical execution costs.

No settlement fee is listed in that document. No authenticated CF passthrough
request was attempted, and no credentials were requested or used.

## Verification

On Linux / Python 3.13.11, **181 unit and regression tests passed**, with socket
connections forbidden throughout the tests. Ruff lint/format, strict Mypy over
37 source files and Git whitespace checks passed.

New tests cover future observations/outcomes, out-of-time calibration,
observation ordering, private historical endpoint blocking at both boundaries,
later-quote execution, one-second latency, preservation of recorded cancellations,
no uncalibrated entries and refusal of candle-only input. Synthetic fixtures are
only software tests; none were installed as a calibration artifact.

GitHub's fork-workflow checks previously required maintainer action; local checks
are not a substitute for claiming a successful remote CI run. No merge or
deployment was performed.

## Reproduction and remaining work

```bash
export KALSHI_ENVIRONMENT=demo
export KALSHI_DRY_RUN=true
export KALSHI_API_KEY_ID=

.venv/bin/kalshi-bot btc-history-audit --limit 200
.venv/bin/kalshi-bot btc-paper --ticks 3 --interval 2 \
  --record data/observations.jsonl
.venv/bin/kalshi-bot btc-replay data/observations.jsonl
.venv/bin/kalshi-bot paper-status
```

These are bounded commands, not a deployed service or a scheduled collector.
Public observations, raw audit responses and reports stay git-ignored under
`data/`; the persistent ledger is not reset or refunded.

To complete empirical validation, obtain timestamped pre-close books with real
depth, sufficiently frequent proxy reference observations, contemporaneous
verified metadata and later confirmed labels. Then predeclare separate
calibration and evaluation periods, audit provenance, replay conservatively and
report confidence/sample limitations. A bounded observation-only collection is
the next credential-free option, but no long-running collection was started.

**Until that evidence exists, BTC entries remain blocked by default.**
Production credentials, real exchange orders, `--live` and authenticated feeds
remain prohibited.

Sources:

- [Kalshi historical data and partitioning](https://docs.kalshi.com/getting_started/historical_data)
- [Kalshi candle schema](https://docs.kalshi.com/api-reference/market/get-market-candlesticks)
- [Coinbase candle schema and granularity](https://docs.cdp.coinbase.com/api-reference/exchange-api/rest-api/products/get-product-candles)
- [Official fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf)
- [Third-party snapshot schema and key-based free-tier limits](https://docs.kalshibacktest.com/available-data)
- [DepthFeed public sample market metadata](https://depthfeed.com/samples/kalshi/kalshi-sample-markets.csv)
- [DepthFeed public sample order books](https://depthfeed.com/samples/kalshi/kalshi-sample-books.csv.gz)
