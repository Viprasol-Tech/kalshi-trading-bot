# Automatic BTC execution foundation — disabled release

Latest offline addition: `offline-campaign-supervisor.md` documents the tested
400-market coordinator (334 offline tests overall). Its fixed deadlines,
cross-window deduplication and quality stops are implemented, but network-driver,
host/provenance integration and approvals remain blocked. No data collection or
entries were started. Earlier test counts below describe their original phases.

12 November 2026. Changes continue the existing Viprasol-Tech repository and
draft PR. **Not ready for automatic trading or an unattended collection.**
No new market observations, deployment, paid resources, paper entries, real
orders or credentials were used. The actual account remains $50 cash with
zero orders/fills, zero fees and zero realized P&L.

## What is implemented

- A sealed release gate, independent of calibration and environment flags.
  Both `btc-paper` and generic `run` refuse before network access.
  `PaperBroker.submit` and `process_quote` refuse direct calls too. There is
  no activation/reset/`--live` escape hatch. A future approval requires a
  separate reviewed release; metrics alone never enable entries.
- A pure automatic opportunity planner that revalidates Kalshi rules and
  recomputes the experimental probability from recent Coinbase proxy ticks.
  It evaluates both YES and NO using actual opposite-side resting asks.
  Caller-supplied probabilities and `passed` flags are not trusted.
- Conservative sizing: one whole contract, <=25% of displayed top-level depth
  (at least four contracts), cent-rounded ask +2 cents adverse slippage,
  >=2 cents fee reserve, <=$1 worst-case purchase loss including fees.
  Require at least 8 percentage points **after** these costs and the empirical
  calibration haircut. Unknown/missing/stale/inconsistent data blocks entry.
  This first execution design requires both sides of the book; one-sided books
  remain separately useful observation data, never invented liquidity.
- A WAL/FULL-sync execution staging journal with deterministic client IDs,
  permanent per-market deduplication, atomic `BEGIN IMMEDIATE` reservations
  and **one global unresolved exposure** across YES, NO and adjacent markets.
  Unacknowledged/rejected submissions retain their reservation for explicit
  reconciliation; no blind retries or automatic risk release.
- Sticky kill switch, initially armed and restart-persistent; no reset API.
  A proposed conservative daily loss limit of **$2** uses UTC gross losses,
  not gains netted against losses; the next trade's full worst-case risk must
  fit within the remaining budget. This is a disabled default to review before
  paper approval, not a change to the $50 account or $1 per-trade cap.
- Detailed opportunity/rejection, staging, acknowledgement and kill events.
  The future paper-submission adapter is present behind the sealed gate.
  It never creates a broker implicitly and also rejects preexisting ledger
  exposure. **Automatic fill/settlement reconciliation and orchestration are
  not release-ready**; no orders will be dispatched by this build.
- An offline prospective campaign registry: immutable protocol and fixed UTC
  cohort boundaries, unique forecast per market across segments, exact retry
  deduplication, conflict rejection, fixed-horizon selection evidence, immutable
  finalized labels and original deadline. Consistent SQLite backup/restore is
  tested. Pending/late pre-test labels are explicitly reported as leakage risk.
  A registry record is not evidence of collection approval, external
  preregistration or data provenance; those remain explicitly unverified.
- A read-only prospective staging evaluator now verifies the manifest fingerprint,
  rechecks fixed cohorts/horizons and declared evidence, reports label availability
  at a cutoff, and excludes late pre-test labels from scoring/baselines. Selection
  scores stay sealed until the test boundary and final-test scores until the
  original campaign deadline. Fixed five-bin ECE, Brier score and log loss are
  reported without fitting or promotion; the test baseline uses only earlier
  development labels. Declared hashes do not authenticate provider evidence,
  and an explicitly supplied offline cutoff is not external time attestation.
  It cannot compute executable edges or snapshot coverage from registry hashes
  alone, so those fields remain unavailable.
- Read-only local dashboard with probability diagnostics for both sides,
  strict net edges and blocking reasons, cash bankroll, realized P&L, fill log,
  settled filled-market win rate, recorded equity drawdown and archive coverage.
  Unknown metrics remain unavailable, rather than fabricated zero performance.

## Calibration: investigated, not “fixed” by fitting the old test set

The original model is unchanged. Original archive, selected forecasts and
chronological scores remain frozen. The eight old test markets have already
been inspected/reported and are **not** a fresh test set for any future model.

| Metric | Development | Held-out historical test |
|---|---:|---:|
| Independent market contracts | 15 | 8 |
| Brier score | 0.14633 | 0.13182 |
| Log loss | 0.44914 | 0.41596 |
| Five-bin calibration error | 17.92 points | 19.24 points |

The earlier development-only investigation found two confident misses, in
opposite directions, contributing 68.32% of development squared loss. This
does not justify a uniform probability adjustment. Proxy/index basis,
non-normal moves, time-varying volatility and sampling remain hypotheses, not
confirmed causes. No synchronized official CF BRTI series exists in this
archive to identify the basis component.

The existing calibration gate fails on sample size, calibration error and
local support. No scaler, volatility adjustment, bin change, feature change,
threshold change or horizon choice was fitted to the old held-out markets.
Synthetic unit fixtures exercise the planner/gates only; they are never
exported as a calibration artifact or counted as independent observations.

The archive has 25 verified markets, 24 finalized outcomes, 23 eligible
fixed-horizon forecast/outcome pairs, 10,759 recorded observations and 9,844
originally complete records (91.50% of recorded; 91.15% of expected slots).
There were three API errors and one recovered host interruption. Frequent
snapshots do not increase independent market sample size.

All 10,759 archived quotes lack actual quote-event timestamps. Therefore
strict executable edges are unavailable for all 23 evaluation markets.
Historical test transport-only maximum net edge was 6.89 points under the old
cost assumptions, **not executable profit or evidence for an 8-point strategy**.
The dashboard reevaluates freshness at viewing time; archive quotes cannot be
relabelled current opportunities.

## Market discovery changes

The preceding readiness patch already restricted listing close-time ranges,
retained actual provider-returned candidates, refreshed cached details,
scheduled close-boundary rollover checks and recorded request parameters.
Missing/unverified `floor_strike` remains rejected; no strike inference from
titles, Coinbase, neighbors or later settlement.

This phase additionally applies the aligned, currently-open interval and
series/ticker identity filter **before the eight-detail refresh cap**, not just
before quarantined-book sampling. Malformed current-looking intervals can no
longer exhaust the request budget ahead of a valid rollover candidate.
An offline test exercises 12 invalid intervals preceding the actual candidate.
The cached detail retry test continues to reject a missing strike until the
fresh provider detail actually publishes it.

The six-hour historical discovery success was 693/815 = 85.03%. Post-patch
discovery success has NOT been measured; these are fixture-verified fixes,
not a new live-data result or a guarantee of upstream metadata completeness.

## Authoritative settlement and fee assumptions

Recognition uses the existing strict Kalshi contract terms:
`https://assets.kalshi.com/contract_terms/CRYPTO15M.pdf`,
CF Benchmarks BRTI, sixty last-minute index samples, arithmetic average rounded
to two decimals, YES at or above the authoritative floor strike. Rules/source,
binary payout, active/finalized status, aligned opening/close times and exact
metadata identity must agree. Unknown rule variants fail closed.

Only Kalshi finalized market result/payout evidence may label evaluation or
settle an eventual approved paper position. Coinbase is explicitly a proxy;
expiry or a spot price is never a settlement instruction. The registry merely
stores externally verified evidence hashes and does not itself certify labels.

The planner requires the verified M=1 quadratic-fee series. A 2-cent minimum
one-contract fee reserve conservatively covers that schedule's formula at
the planned limit. Two cents slippage and 25% depth participation are explicit
conservative simulation assumptions, not measured fills or guaranteed costs.

## Sufficient observations and unseen evaluation

Target **400 NEW eligible finalized market contracts**, ideally >=100
development, >=100 chronological model/calibration selection and >=200
untouched final-test contracts. Two hundred total contracts is a data milestone,
not 200 independent test cases. Adjacent BTC intervals are correlated; report
time-block uncertainty and regime coverage as well as contract counts.

Before collection, freeze a timestamped prospective manifest: original start/
deadline, fixed UTC cohort boundaries, model/source/config/ledger hashes,
300 +/-30-second primary horizon, one forecast per ticker, exclusion/tie-break
rules, costs, calibration/decision gates, permitted recovery and spend limits.
If fixed-window sample quotas are missed, report failure/insufficiency rather
than moving boundaries after observing results. Fitting/selection labels must
be finalized and available before test forecasts; embargo pending labels.

Report Brier, log loss, fixed five-bin ECE, baseline from only prior available
labels, local-bin support, both hypothetical cost-adjusted edges, valid/stale/
missing coverage, exclusions, finalized/pending counts, API errors and
interruptions. Hypothetical edges are not fills or realized returns.

The theoretical 400-contract floor is 100 hours before gaps/delays. Proposed
maximum seven days requires a reviewed global supervisor over bounded <=6-hour
collector segments. The new registry does not run these segments; it supplies
local immutable identity/cohort tracking. It never extends a deadline.

## Durable storage, hosting and cost blockers

No new campaign was started: the previous conversation host stopped once
despite keep-alive. Browser/phone-independent operation is NOT guaranteed here.
The requested “collect sufficient data” therefore remains blocked, not silently
run in an unreliable shell or on unapproved paid hosting.

Local WAL/FULL-sync, single-writer transactions, restart deduplication, sticky
stops, original deadlines and consistent backup/restore are tested. These do
not prove cloud durability, zero-loss recovery, external registration or
continuous unattended hosting.

Still required before launch:

1. Confirm the actual repository-linked always-on host and an account-specific
   quote/spend control. Read-only inspection of the two project-search candidates
   found different application/source layouts and no matching repository remote
   or requested Python collector; neither is assumed to host this repository.
   Neither project was modified or launched. No linked deployment is verified.
2. Implement/verify durable remote authoritative storage (recommended managed
   PostgreSQL with SQLite exports), collector adapter and cross-segment
   supervisor. Alternatively approve a nonzero-RPO consistent SQLite backup
   design with distributed single-writer coordination.
3. Prove clean-instance restore after worker/host loss, external heartbeat,
   clock checks, terminal quality-stop refusal and no deadline extension.
4. Solve the actual quote-event timestamp feed limitation without production
   credentials or freshness relaxation. New data without event timestamps may
   aid forecast calibration but cannot validate executable freshness/edges.
5. Obtain separate explicit approval for any hosting cost/deployment, with
   campaign start/deadline and a verified shutdown that also stops billing.

Prior published price estimates remain estimates, not a purchase: 0.5-vCPU/
2-GiB Reserved VM $15/month (hourly equivalent about $3.49 for seven days,
do not promise prorating); continuously active single database compute unit
about $26.88/week plus storage, transfer and backups. Proposed $50 infrastructure
ceiling is NOT authorization, a confirmed account quote or a guaranteed bill.
Recheck prices, credits, machine scale, subscriptions, taxes and spend controls
before approval. Existing detailed references are in
`validation-readiness-20261112.md`. No paid resources were created.

## Dashboard and stop-latch commands

After installing existing dependencies:

```sh
KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true \
  kalshi-bot btc-dashboard --ledger data/paper.db \
  --observations data/btc-observe-6h-20261112.db
```

This serves `http://127.0.0.1:8765` **on the machine running the command**.
It is not a public iPhone-accessible deployment. Read-only HTML and JSON,
loopback binding, Host validation, no-store, no third-party scripts and no
trading/control POST endpoints. No market polling occurs. Public access needs
authentication and a separately approved deployment; do not expose this
local server on the internet as-is.

```sh
kalshi-bot btc-kill --reason "operator stop"
```

This creates/updates only the separate control journal's sticky stop latch;
it does not modify bankroll or orders, promise hosting shutdown, or liquidate
positions. There are presently no orders/positions to cancel. All actual
submission/fill paths remain locked regardless of the latch.

After a separately approved campaign has supplied a verified registry:

```sh
KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true \
  python -m kalshi_bot.btc_campaign_validate data/campaign.db \
  --output data/campaign-evaluation.json
```

This command opens the existing registry read-only and exclusively creates a
new output file. Missing input is never created; existing output is never
overwritten. It never starts collection, changes the account, exports a
calibration artifact or enables entries. No real campaign registry was created
or scored in this phase; synthetic fixtures are not market observations.

The companion HTML snapshot is an actual archive/ledger rendering, not a live
feed or a substitute for the included dashboard server. No performance series
or trades have been fabricated to populate it.

## Verification and release decision

261 offline tests passed; Ruff lint/format, strict Mypy over 44 source files
and diff-whitespace checks passed. A 200-tick isolated synthetic momentum
regression completed (-$0.48 hypothetical net P&L); it is not BTC strategy
evidence and did not modify the persistent account. The model, archive and
original evaluation SHA-256 values match the frozen pre-patch fingerprints.
Dashboard rendering against the actual archive loaded 30 archived rows with
zero strict executable opportunities, no read errors and no account changes.
Remote CI is reported separately; local passes do not imply hosted CI passed.
Tests include independent sealed-gate coverage without simulation test overrides,
both-side planning, stale/missing/invalid data, cent/risk limits, restart-safe
deduplication/global exposure, daily gross-loss budgets, sticky kills, fixed
cohorts, conflicting labels, backup restore and read-only dashboard behavior.
Legacy simulator tests explicitly use a test-only fixture; runtime has no such
override. Only explicitly isolated in-memory synthetic backtests remain usable.
The additional 16 prospective-scoring tests cover sealing, fixed-deadline
unsealing, exact scores, prior-only baselines, delayed labels, corrupt evidence,
invalid cutoffs, missing input and read-only behavior.

This is **implemented staging and a dashboard, not a completed validated
automatic trader**. The collection supervisor/durable remote adapter, actual
new-data campaign, empirical validation, authoritative order/fill/settlement
reconciliation, release evidence review and explicit paper approval are still
mandatory. Live execution is not implemented/enabled and requires a separate
future decision. Never infer approval from good metrics.
