# BTC-first validation and metadata integration — unreleased

This extends existing draft PR6, the BTC strategy, the shared paper engine and
the 400-market supervisor. It does not replace the application. No collection,
paper session, exchange order, production credential, host purchase or deployment
was started. Production writes remain permanently blocked on this branch.

## Implemented in this revision

- `btc-validation-manifest` reads the original paper ledger without modifying it
  and emits a canonical SHA-256-bound **review artifact**, not a release token.
  It captures every Python source file, the current supervisor implementation
  fingerprint, unchanged experimental BTC model and untouched $50 ledger.
- The manifest specifies 100 development, 100 selection and 200 new untouched
  test markets; one outcome-independent 300±30-second forecast per ticker;
  fixed chronological UTC boundaries, original deadline and no holdout reuse.
  The already examined six-hour archive is excluded from fresh test cohorts.
  `schedule_manifest()` creates a review-only Protocol compatible with the
  existing Supervisor/DemoBridge. Times are deliberately unset in the delivered
  draft: there is no running campaign or approved deadline.
- Existing DemoBridge remains connected to the strict BTC Collector and one-DB
  supervisor, including raw receipts, rollover, proxies and final labels. It is
  still sealed before feed IO. No new collection escape hatch was introduced.
- Shared discovery now follows `market.event_ticker` → `GET /events/{id}` →
  returned `event.series_ticker` → `GET /series/{id}`. It no longer derives the
  series ID by splitting the event ticker. Returned identities must match.
  Event/series metadata and actual provider category text persist in SQLite;
  unknown labels stay visible but unsupported, not guessed from market titles.
  The internal category allowlist is routing policy, not a claim those labels
  or markets exist on the demo venue.
- Event-specific fee overrides, provisional markets, conflicting event/series
  categories, unknown rules/fees and missing/unverified BTC floor strikes block
  qualification. Fee overrides require a reviewed adapter, not a default fee.
- Missing prices/depth/timestamps remain missing. Conflicting explicit quote
  timestamps now fail closed instead of taking the first convenient field.
  HTTP Date, market updated_time, generic timestamp and receipt time are not
  quote-event evidence. Existing <=5s event age, <=2s event-to-receipt latency,
  <=60s metadata age, >=4 actual contracts per side and <=10c spread are retained.
- Shared settlement now requires Kalshi **finalized** status, consistent with
  the BTC collector, not a synthetic `settled` status. Official result, payout,
  rules, identities and settlement/close times must match before crediting.
- Read-only mobile dashboard now includes pending/filled/cancelled order
  records alongside opportunities, positions, fees, realized P&L, observed
  drawdown and category win rates. No activation controls were added.

Kalshi reference documentation:
[Get Event](https://docs.kalshi.com/api-reference/events/get-event) supplies
`event_ticker`, `series_ticker`, category and optional fee overrides.
[Get Series](https://docs.kalshi.com/api-reference/market/get-series) supplies
category, settlement sources and fees. Documentation is not proof of current
demo coverage or quote timestamp availability; no new market-data probe ran.

## Offline use, not launch instructions

```text
kalshi-bot btc-validation-manifest --ledger data/paper.db
kalshi-bot multi-status --ledger data/multi-paper.db
```

The first prints JSON only; redirect to a new review file if desired. The second
does not initialize a missing ledger. No campaign, dashboard server, paper
runtime or credentials are created by either inspection command.

`verify_manifest()` detects changed sources/ledger/policy and refuses flags that
pretend to grant approvals, even if the caller recomputes the JSON hash.
Hashes are integrity checks, not external provenance/signatures. A scheduled
draft is not immutable external attestation; the existing supervisor persists
its frozen manifest and rejects changed schedules on resume.

## Evidence and model status

All six category releases remain blocked. BTC has an unchanged experimental
model; the other five categories have no probability model or matched independent
historical dataset. Dynamic market inventory is not a probability model. No
forecast probabilities, timestamps, prices, independent observations, calibration
scores or profits were fabricated to fill this gap.

| Category | New independent finalized markets | Validated model | New Brier / log loss / ECE | Real-data paper trades / win rate / P&L / drawdown |
|---|---:|---|---|---|
| Crypto / BTC | 0 | No | N/A | 0 / N/A / N/A / N/A |
| Sports | 0 | No | N/A | 0 / N/A / N/A / N/A |
| Economics / Fed | 0 | No | N/A | 0 / N/A / N/A / N/A |
| Weather | 0 | No | N/A | 0 / N/A / N/A / N/A |
| Politics | 0 | No | N/A | 0 / N/A / N/A / N/A |
| Financial | 0 | No | N/A | 0 / N/A / N/A / N/A |

Historical BTC six-hour results remain diagnostic only: 8 already-examined test
markets, Brier 0.13181958, log loss 0.41595637, five-bin ECE **19.24 percentage
points**. They were not tuned or recycled. No archived strict executable edge
was eligible because actual quote-event timestamps were missing.
The original ledger remains $50 with zero orders/fills/fees/realized P&L.
Missing trial results are N/A, not a claim of zero-return strategy performance.

## Exact remaining prerequisites, in order

1. Review this revision and its current-source CI. A maintainer must approve the
   upstream workflow; a green fork run is not upstream approval.
2. Approve a bounded **observation-only** demo preflight. Verify actual available
   BTC market metadata, rule/strike completeness, both-sided liquidity and
   quote-event timestamp semantics. The prior single sampled book lacked an
   explicit event time. If REST cannot supply verified event timestamps, prove
   a demo-only read-only WebSocket snapshot/delta adapter and sequence recovery.
   Authentication, if required, needs a separately reviewed demo credential path;
   no production key or receipt-time fallback is permitted.
3. Select and separately approve host costs and deployment. Verify durable disk,
   off-site backup/restore, reboot/restart, UTC clock, health/alerts and private
   HTTPS/authenticated iPhone access. Current dashboard is loopback-only; there
   is no deployed iPhone URL or continuous-runtime guarantee.
4. Preregister numerical acceptance thresholds, baselines, uncertainty analysis,
   feature/source provenance and chronological schedule **before reviewing new
   selection/test performance**. Freeze a new current-source manifest, then
   separately release observation collection. No date or approval is invented
   by this revision.
5. Obtain at least 400 new eligible finalized BTC markets at fixed forecast
   horizon with independently verified pre-close data, official labels and no
   cross-cohort event overlap. Count correlated time blocks, not ticks, as well
   as contracts. At 96 BTC fifteen-minute markets/day, 400 is a theoretical
   >=100 hours; filtering, outages and label delays increase that time. Existing
   maximum campaign is seven days; missing quotas mean failure, not an extension.
6. Evaluate development/selection and untouched test chronologically. Report
   Brier, log loss, ECE, baseline comparisons, uncertainty, coverage, independent
   test counts and both-side conservative after-cost edges. More labels alone
   do not prove a model profitable or justify a model-release bypass. Each
   additional category needs its own rule-aware model and independent evidence.
7. Review a separate paper release after reconciliation, kill, duplicate-order,
   restart and data-quality acceptance. Current whole-contract simulator
   supports later-quote entries/exits and finalized settlement; it is not a
   realistic fractional partial-fill/queue-position or live exchange simulator.
   Prove any required fractional-fill behavior before claiming that readiness.
   No automatic promotion is implemented from scores, JSON flags or CI results.

The future separate paper trial retains $50 starting cash, <=$1 full entry risk,
$2 UTC gross loss **plus unresolved risk**, >=8pp estimated edge after fees,
2c adverse slippage each direction and uncertainty reserve; <=3 pending/held
positions, <=1/category and correlated group, unique intents and sticky kill.
Gross profits never replenish the same-day loss budget.

## Verification

Local: **486 passed, 0 skipped, 12 warnings, 88% statement coverage**; Ruff
lint/format (115 files), strict Mypy (54 source/wrapper files), whitespace checks
passed. Forty new synthetic/offline cases cover the manifest and discovery
boundaries; they are not independently acquired market observations. Existing
local native Caddy, restic backup/recovery and systemd syntax drills ran.
Warnings include deprecated websockets legacy imports and SQLite ResourceWarnings.

The delivered readiness report records the separate actual hosted results for
this revision. Existing green hosted CI for source `0354449` does not cover changed
code. Offline mocks/synthetic fixtures prove mechanisms, not data availability,
historical model calibration, actual fills, continuous hosting or profitability.
Real API, credentialed WebSocket, VPS/reboot/off-site network and independent
model-validation tests are not performed by the offline suite.
