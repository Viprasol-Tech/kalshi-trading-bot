# BTC validation readiness after the six-hour observation run

Reviewed 12 November 2026. Existing Viprasol-Tech repository and draft PR retained.
**Decision: improved offline readiness, but NOT READY for a new unattended
collection or for paper/live entries. No hosting or collection was started.**

## 1. Frozen evidence and calibration investigation

The completed authorization was 2026-11-12 00:59:36.086 to 06:59:36.086 UTC.
One host interruption occurred despite keep-alive. The fixed deadline was not
extended; 41 two-second slots went unattempted. No requests completed after
the deadline. The original SQLite archive and original evaluation remain
unchanged. The probability model source is byte-for-byte unchanged.

| Existing result | Development | Chronological test |
|---|---:|---:|
| Distinct eligible forecast/outcome markets | 15 | 8 |
| Brier score | 0.14633 | 0.13182 |
| Log loss, natural logarithm | 0.44914 | 0.41596 |
| Five-bin expected calibration error | 17.92 percentage points | 19.24 percentage points |
| Prior-label prevalence baseline Brier | Not used | 0.25087 |

There were 25 verified markets, 24 finalized outcomes and 23 eligible
five-minute-horizon forecast/outcome pairs. The initial partial market had no
forecast in the prespecified 300 ±30 second window; the final market lacked a
finalized outcome at cutoff. Eight unique test contracts are not 9,844
independent samples, and adjacent BTC contracts are not guaranteed IID.

The test result is frozen, not a tuning target. No probability scaling,
volatility multiplier, clipping, bin-boundary change, feature selection,
forecast-horizon selection or recalibration was chosen using these test labels.
The original probability/label records, model source, archive and evaluation
have recorded SHA-256 fingerprints. A regression comparison confirms that
development/test scores and all selected probabilities/outcomes are identical.
The old test set has been reported and is no longer a fresh holdout for choosing
subsequent model versions.

### Development-only diagnostic findings

The development cohort already exhibits substantial calibration error. Two
development misses account for **68.32% of its total squared forecast error**:

- KXBTC15M-26NOV112215-15: predicted YES 91.87%, finalized NO.
- KXBTC15M-26NOV112300-00: predicted YES 19.04%, finalized YES.

Development mean prediction was 40.95% versus a 46.67% YES frequency. That
aggregate underprediction coexists with large confident errors in both
directions; it does not establish that one uniform probability adjustment
would solve the problem.

The model uses a driftless local-normal diffusion and a trailing proxy
volatility estimate, with an approximation for the last-minute arithmetic
average. Coinbase is not CF BRTI. Time-varying volatility, non-normal moves,
proxy/index basis differences and sampling effects are plausible contributors,
**not identified causal explanations** from these 15 markets. No synchronized
official CF BRTI series was collected, so basis error cannot be measured here.

The eight-test-market ECE is especially unstable: nonempty bins have only
1, 1, 2 and 4 observations. The largest reported bin gap is 38.86 points in a
single-observation bin. Do not re-bin, remove that market or optimize a
threshold against it. The test ECE exceeds the existing 5-point gate, while
the 200-market gate and local-bin support requirements also remain unsatisfied.
A lower test Brier than the baseline does not override any of those gates.

## 2. Discovery and rollover changes implemented

The six-hour run had 815 discovery attempts: 693 successful and 122
unsuccessful (85.03% success). Failed attempts included 119 current candidates
without an authoritative `floor_strike` and three unavailable/stale lists.
These attempt counts are not counts of permanently lost markets.

Implemented in the existing observer:

1. Restrict market listings with documented `min_close_ts` and `max_close_ts`
   to the current and next 30 minutes, retaining pagination/cursor bounds.
   Do not rely solely on an open-status filter.
2. Retain actual provider-returned upcoming candidates, never invented tickers.
   During a short retry window, refresh current cached candidates directly
   instead of repeatedly listing the entire series. A cached candidate always
   needs a fresh detail response before acceptance. Full lists refresh at most
   30 seconds apart when a current cached candidate is available.
3. Preserve close-boundary refresh, <=5-second near-rollover discovery, bounded
   missing-market backoff, fresh series verification and immutable identity.
4. Fix quarantine ordering: filter valid current time intervals **before**
   applying the eight-candidate request cap. Previously unrelated/future
   candidates could consume the cap. In a bounded check of the immediately
   preceding archived list for failed attempts, a current candidate existed
   in 121 cases and fell outside the first eight in 98. This is not a full
   pagination reconstruction and does not prove all old gaps recoverable.
5. Persist request query parameters in a separate SQLite table so future
   range-filter and pagination behavior can be audited. Historical archives
   did not record query parameters; they are not backfilled.

Missing, nonfinite, nonpositive, imprecise or unverified strikes are still
rejected. Exact CF BRTI rules/source/rounding, active status, opening/closing
times, payout and identity checks remain mandatory. Coinbase, subtitles, later
settlements and neighboring contracts are never substitutes for a strike.
Unavailable upstream metadata is still a gap, not something this patch can
guarantee away. Quarantined books are explicitly unverified, have no model
strike and cannot become forecast/execution evidence.

The new discovery paths are tested offline only. No new public market run
was performed to validate a post-patch rollover.

## 3. Strict freshness, asks and hypothetical edges

Original coverage:

| Measure | Result |
|---|---:|
| Expected two-second slots | 10,800 |
| Recorded observations | 10,759 |
| Complete valid records under original transport/reference checks | 9,844 (91.50% of recorded) |
| Incomplete/missing flags | 913 (8.49%) |
| Stale flags | 2 (0.019%) |
| Complete valid records / expected slots | 91.15% |
| One-sided books | 348 |
| Empty books | 5 |
| Unavailable books | 560 |
| Public request errors | 3 Kalshi ReadTimeouts / 33,310 requests |
| Coinbase proxy references passing checks | 10,759 / 10,759 |
| Actual quote-event timestamps available | **0 / 10,759** |

HTTP Date, receipt time and market `updated_time` are not quote-event timestamps.
Original "valid" counts therefore do not prove that a resting quote changed
within five seconds. Unknown event age stays unknown.

New strict executable-edge diagnostics require:

- Explicit supported quote-event timestamp and provenance, matching its raw
  value, not in the future, <=5 seconds old at receipt and evaluation.
- Book/reference/metadata receipt age <=5 seconds, HTTP age from -1 to 5 seconds,
  latency <=2 seconds, and a valid Coinbase reference.
- An ask reconstructed from the actual opposite-side resting bid ladder,
  matching stored ask/depth, with at least **one full contract** available.
  Fractional depth is not rounded up; missing quotes stay null.
- One-contract M=1 quadratic taker fee, rounded up to a cent, with a minimum
  $0.02 fee reserve, plus **$0.02 assumed adverse slippage**. These conservative
  constants are execution assumptions, not fitted model parameters or empirical
  fill evidence. No model probability is changed.

An explicit stale quote now clears inherited edge/quote-eligibility fields.
Unknown event time also blocks quote eligibility. Strict diagnostic fields are
separate from the original observer's non-actionable historical diagnostics.

| Frozen historical transport-only diagnostic | Development | Test |
|---|---:|---:|
| Lowest side-level net edge | -15.87 points | -12.27 points |
| Highest side-level net edge | +9.50 points | +6.89 points |
| Side diagnostics >=8 points | 1 | 0 |
| Markets eligible for the new strict executable-edge calculation | 0 | 0 |

Historical diagnostics used the exact rounded M=1 fee plus $0.01 assumed
slippage. They are retained for comparison, **not relabeled executable**.
Under the new strict freshness/depth policy, edges are null for this archive,
not zero or profitable. Even future qualifying asks do not imply fills.
If public REST never supplies event timestamps, strict execution readiness
remains blocked. A documented credential-free timestamped feed or a separately
approved policy decision would be needed; no authenticated/production feed or
freshness relaxation has been enabled.

## 4. Proposed observation-only campaign, not authorized or started

Collecting 200 distinct finalized markets is a minimum data milestone, not
enough to claim that 200 were held out. A 2/3 split of 200 leaves only 67 test
markets. The safer target is **at least 400 new eligible finalized markets**:

1. First 100 new markets: development only.
2. Next 100: chronological calibration/model-selection validation only.
3. Freeze model, selection, costs, gates, missingness policy and cohort boundary.
   All fitting/selection labels must have finalized and been received before
   the first final-test forecast. Use an embargo if necessary.
4. Next 200 eligible markets: untouched chronological final test. No label
   inspection or score-based early stopping until the preregistered cutoff.

The already reviewed archive is development history, never recycled as a new
holdout. No fitting or strategy activation is authorized by this plan.

The theoretical lower bounds are 50 hours for 200 contracts and 100 hours for
400, before missing forecasts, outages and delayed labels. Propose a **seven-day
maximum** (672 nominal 15-minute intervals), with a fixed global deadline and
no automatic extension. Finish based on the predeclared eligible count, not
whether scores look favorable; stop sooner for quality/safety violations.
Final labels not received by the cutoff remain pending. Any extra label-only
window must be explicitly approved in advance.

Preregister one primary horizon: 300 ±30 seconds before close, same one-forecast
selection tie break, one market counted once. Other logged horizons are
secondary diagnostics, not extra independent markets or an opportunity to pick
the best horizon after testing. Local probability/horizon bins still require
at least 30 observations; 200 overall may not meet that support requirement.
Use future whole-time blocks for uncertainty analysis and report dependence
and regime concentration, not just naive snapshot-level confidence intervals.

The evaluator now accepts a fixed timezone-aware `--split-at` boundary and
`--as-of` cutoff. Tests show later label arrival cannot move existing records
between cohorts. The default historical 2/3 split is retained for reproducibility.
A supplied boundary alone does not prove preregistration: that provenance is
reported as unverified until a frozen, timestamped prospective manifest exists.

Before a campaign, implement a global manifest with approved start/deadline,
model/source hashes, ordered cohort membership, source/rule identity,
selection/exclusion rules and maximum restart/cost limits. Do not silently run
this with `--seconds 604800`: the collector deliberately still refuses more
than six hours. A reviewed campaign supervisor and cross-segment deduplication
are required. Bounded six-hour segments would limit memory/summary size; 28
segments fit seven days. Unique market/forecast identity must span segments.

## 5. Durability, recovery, hosting and costs

### Verified here

- Original account logical digest unchanged: $50 initial/current, $0 realized
  P&L/fees, zero orders and fills. No broker is constructed by the observer.
- Full SQLite backup API snapshot of the completed 262,328,320-byte archive
  restored successfully; integrity and all nine table row counts matched.
  Original manifest/deadline and source bytes were preserved.
- Offline snapshot/restart tests verify lock release, interrupted-slot
  handling, original deadline, source/ledger guards and duplicate suppression.
  Existing tests also exercise unclean process death and terminal-stop refusal.
- The actual previous host interruption recovered with zero duplicates and
  no deadline extension. Terminal quality/safety stops remain non-resumable.

### Not verified, therefore launch blockers

- This conversation host failed to remain awake despite keep-alive.
- No unambiguously linked deployment for this repository was identified in
  project search. No Reserved VM, paid database or storage bucket was created,
  inspected as a confirmed target, or charged by this task.
- No cloud restore/redeploy/host-loss test, external heartbeat, distributed
  single-writer lease, supervised restart or real billing quote is verified.
- The local SQLite snapshot demonstrates file recovery, not replicated
  durability, zero-loss recovery or production hosting reliability.

For a Replit deployment, recommend a **Reserved VM background worker**, not
an idle Autoscale process or an editor shell. Official docs describe Reserved
VM as continuously running, but also warn not to rely on the published app's
local filesystem. **Reserved VM alone does not make local SQLite durable.**

Preferred durable design: managed PostgreSQL as authoritative committed
observations/manifest/unique keys, with verified read-only SQLite exports and
App Storage backups. This needs an adapter; it is not currently implemented.
An alternative local-SQLite design needs scheduled consistent backups, a durable
manifest, verified restore, single-writer coordination and an explicit nonzero
recovery-point allowance. Never copy just an active `.db` while ignoring WAL.

Before starting, test on the selected host: terminate the worker, restore into
a clean instance, verify ledger/hash/counts/deadline, restart without duplicates,
refuse terminal/expired runs, verify clock synchronization and public endpoints,
and prove phone/browser closure does not own the process lifetime. Alert on
stale heartbeats and backup failures. Do not auto-restart a quality stop.
Any preflight network collection, deployment or charge requires user approval.

### Published price estimates, not an account-specific quote

Sources checked 12 November 2026:

- [Publishing prices](https://docs.replit.com/billing/deployment-pricing):
  0.5 vCPU / 2 GiB shared Reserved VM **$15/month**.
- [August 2026 cloud rates](https://docs.replit.com/billing/aug-cloud-billing-updates):
  same-size hourly rate **$0.0208/hour**. The $0.0104 configuration is **legacy**;
  do not assume it can be newly selected.
- [App Storage rates](https://docs.replit.com/billing/object-storage-billing):
  $0.015/GiB-month, $0.05/GiB transfer, basic operations $0.0004/1,000,
  advanced operations $0.005/1,000; seven-day minimum object billing.
- Managed production database compute listed at **$0.16/compute-hour** and
  storage **$0.35/GiB-month** in the cloud-rates page.

Seven-day arithmetic: VM hourly equivalent about **$3.49**, but do not promise
prorating; provision up to the full **$15** until the actual quote is checked.
A continuously active single database compute unit for 168 hours is about
**$26.88**, plus storage/transfer/backup operations. At the old archive's growth
rate, seven days would be about **6.84 GiB** uncompressed; full-month object
retention for one such copy is about **$0.10** and one 6.84-GiB transfer about
**$0.34**. Backup frequency/retention multiplies storage and operations.

Suggest an initial **$50 infrastructure approval ceiling**, not a guaranteed
bill: VM up to $15 + estimated database compute $26.88 + storage/transfer
headroom. Verify the actual machine/database scale, minimum charges, credits,
taxes, subscription requirements and a spending control before authorization.
This excludes Agent/model work, subscriptions and paid market/reference feeds.
Stopping the Python process does not necessarily stop deployment billing:
an approved campaign shutdown must explicitly stop the paid resources.
Cost controls are not yet configured or verified.

References: [deployment types](https://docs.replit.com/features/publishing/deployment-types),
[filesystem warning](https://docs.replit.com/learn/projects-and-artifacts/replit-deployments),
[storage options](https://docs.replit.com/learn/projects-and-artifacts/storage-and-databases),
[Kalshi market filters](https://docs.kalshi.com/api-reference/market/get-markets).

## 6. Readiness decision and next steps

Offline implementation checks: 203 tests passed; Ruff lint/format, strict Mypy
and diff-whitespace checks passed. Discovery, strict-edge, fixed-boundary and
snapshot/restart checks used isolated fixtures, not a new market collection.
Remote CI status must be reported independently, not assumed passed.

1. Review this patch without tuning the old test set.
2. Approve a specific durable-storage/supervisor implementation and a final
   prospective cohort protocol; do not deploy it yet.
3. Verify the actual paid-host quote and recovery preflight, then request a
   separate explicit approval for any hosting/preflight/campaign spending.
4. Only after that approval, collect observations; report counts, missingness,
   sources, finalized labels, fixed-test Brier/log loss/ECE, strict edge
   eligibility and all errors/interruptions before considering another stage.

Keep `KALSHI_ENVIRONMENT=demo`, `KALSHI_DRY_RUN=true`, no production trading
credentials, no `--live`, no order submission and no simulated entries.
The $50 account, future $1 risk cap and 0.08 minimum net edge remain unchanged.
No calibration artifact, promotion, new collection, paid deployment or trading
is authorized by this report.
