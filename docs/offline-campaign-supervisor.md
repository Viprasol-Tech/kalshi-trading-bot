# Offline 400-market campaign supervisor

Implemented for the existing Kalshi repository. **No collection started.**
No hosting purchased/deployed, demo credentials connected, synthetic entries
placed in the user's account, paper trial enabled or live trading enabled.

## What this release does

`kalshi_bot.btc_supervisor` coordinates one immutable prospective campaign across
fixed six-hour windows, using **one persistent campaign SQLite database** for
the original protocol, observations, candidate forecasts, finalized outcomes,
cross-window counters, checkpoints, events and the existing evaluation registry.
It accepts explicit offline frames and host-health facts; it does not poll the
network, launch a process, sleep through a campaign or install a service.

`run_collection()` deliberately always refuses. There is no launch CLI, service
unit, environment override, approved-host marker or calibration artifact that
enables collection or entries. This is an implemented/tested **offline coordinator**,
not a deployed or operational 400-market collector.

### Original time boundaries and restart

- Fixed global start/deadline, at most seven days; protocol requires 100 development,
  100 chronological selection and 200 untouched test markets.
- Six-hour windows are derived from the original start, never from a new process
  start. A final shorter window ends at the original global deadline.
- One global two-second slot ID per frame. Exact retries do nothing; conflicting
  retries arm a permanent safety stop. Missing outage slots are counted, not
  backfilled or relabeled as successful observations.
- Single-host kernel file lock prevents competing supervisors. SQLite WAL,
  FULL sync and immediate transactions commit evidence, candidates, counters,
  registry updates and checkpoint state together. Recovery does not reset quality
  clocks or move cohort boundaries.
- Wall/monotonic drift over two seconds, any monotonic reversal, a backward resume
  wall clock, changed source/model/account fingerprints or unsafe environment stop
  processing. Each process also has a monotonic cap for the remaining original
  deadline.
- Completed, quality-stop, safety-stop and operator-stop states cannot be resumed
  or cleared. Stop states are idempotent; there is no reset method.
- Achieving 400 labels does **not** move boundaries, unseal tests early, promote
  a model, enable entries or launch anything. The window ends at its original
  deadline and can report insufficient quotas.

### Data handling and conservative stop conditions

Raw market metadata is passed through the existing strict series/rules/strike/
status/open-close verification. Missing or unverified `floor_strike` is rejected;
no inference from a title, Coinbase, neighboring market or later metadata.

Quote/reference/metadata transport evidence, actual quote-event timestamp,
raw opposite-side executable asks and depth are rechecked at **receipt time**,
not merely the caller's declared observation time. Missing/stale timestamps,
inconsistent depth and incomplete books are never repaired with fabricated data.
One-sided books are counted separately: useful observation data is not mislabeled
a complete two-sided observation or trading permission.

The frozen original model recomputes probabilities from supplied Coinbase proxy
ticks. A caller's probability/healthy flag is not used as prediction evidence.
Forecast selection is fixed: nearest to 300 seconds before close, within ±30
seconds, earlier timestamp breaks ties. Candidates freeze only after the horizon
window plus a two-second bounded receipt allowance; the decision never considers
the outcome or model score. One frozen forecast per ticker spans all windows.

Finalized outcomes require the already verified market identity/rules/strike,
Kalshi finalized status/result, matching binary payout and authoritative settlement
timestamp. Expiry or a Coinbase spot price never supplies a label. All verified
market outcomes can be retained, even when warm-up produced no eligible forecast.
Retries retain the first receipt timestamp and original evidence. Neutral metadata
updates do not relabel a market; conflicting economic outcomes cause a safety
stop. No new evidence is ingested at or after the original deadline.

Quality clocks, rolling history and consecutive provider-error counters persist
across six-hour boundaries and restarts:

- No healthy data for over 60 seconds; no verified market for over 180 seconds.
  This supervisor conservatively applies the 60-second guard even during a
  metadata rollover gap; it is stricter than the legacy observer's rollover grace.
- More than 40% unusable **or missing** slots in the last 150 expected slots.
- More than 10% stale observations within that same expected-slot window.
- Ten consecutive failures from either provider. Ordered successful results reset
  only that provider's counter; a reconnect/segment boundary does not.
- Missing/old host-health evidence (>5 seconds), clock offset over one second,
  off-site backup success older than 15 minutes, less than 2 GiB or 15% free disk.

These host facts are **declared offline inputs**, not authenticated telemetry.
Actual disk/clock/backup probes, stream provenance and the existing collector
must be wired and acceptance-tested before any separately approved collection.
The legacy collector remains <=six hours; this module does not silently wrap it
in new six-hour authorizations or extend its manifests.

### Evaluation and reporting

The existing `CampaignRegistry` now shares its validated insert operations with
the supervisor's enclosing transaction. Its public methods remain compatible.
The existing chronological evaluator and sealed-test behavior are unchanged.

Supervisor reports contain:

- Original deadline, persistent stop reason and fixed segment windows.
- Complete, useful one-sided, incomplete and stale observations; unrecorded slots
  and missing/incomplete percentage through the checkpoint.
- Verified distinct markets, finalized verified outcomes and eligible finalized
  forecast counts by cohort. Pre-test labels arriving at/after the test boundary
  are not counted toward score-eligible pre-test quotas.
- Discovery attempts/success rate, recorded API failures and explicit resume count.
- False authorization/provenance/validation/collection/entry/live flags.

Counters update atomically per receipt, avoiding repeated full scans of a growing
raw archive merely to show status. Missing discovery attempts are unknown, not
fabricated failures or successes. A resume event reports the unobserved gap;
it is not proof of exact host outage duration. Distinct contracts are not proof
of statistical independence: time-block uncertainty/regime coverage remain needed.

This module does not fit a model, print held-out scores, produce a calibration
artifact or claim profitability. Brier/log loss/calibration remain the task of
the existing read-only evaluator after the fixed chronology permits evaluation.

## Verification

**334 offline tests passed**, including 36 new supervisor tests. Ruff lint/format
and strict Mypy on 49 source/wrapper files passed; total statement coverage was
87%. The local restic/Caddy/systemd drills were run again, not skipped. Warnings
remain for the deprecated WebSocket legacy client and SQLite connection resource
cleanup in the coverage-enabled suite; no warning was suppressed.

New synthetic tests exercise deduplication, fixed-cohort selection, probability
recomputation, unchanged $50 ledger, two-process locking, crash rollback/retry,
fixed six-hour boundaries after restart, cross-boundary quality history,
clock drift/reversal, missing strikes, timestamp freshness at receipt, one-sided
coverage, ordered provider failures, finalized/neutral/conflicting settlements,
no post-deadline insertion and clean-backup restore without deadline extension.

An explicitly synthetic 400-label registry fixture checks 100/100/200 quotas and
test-score sealing; it has **zero authenticated market observations** and cannot
unlock collection or trading. Synthetic labels are not counted as real acquired
samples or exported to the user's campaign database.

The actual account/model/archive/evaluation fingerprints are verified separately.
No new real campaign database is initialized, and no original held-out test
markets are reused or tuned.

Actual account verification: initial/current cash 5,000 cents; orders, fills,
fees and realized P&L all zero. Logical account digest remains
`8177f821ee9f50524a52380d1217a5cfa7d7ac1626e8367ecc193dd7e9581d1e`.
The original probability-model, six-hour archive and evaluation hashes are
unchanged. This phase supplies no new empirical performance result.

## Offline inspection

After a separately reviewed offline fixture or eventual approved campaign exists:

```sh
KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true \
  python -m kalshi_bot.btc_supervisor EXISTING_CAMPAIGN_DATABASE
```

This CLI opens an existing database **read-only** and prints control state. It
does not construct a supervisor, create a database, launch collection, change
the account or enable entries. The deployment proposal remains deliberately
non-runnable with null times/hashes and all approvals false.

## Recovery and remaining launch work

The DigitalOcean backup includes `campaign.db`, so its manifest/control/quality/
dedup/forecast/outcome tables restore together. Local clean-directory recovery is
tested. **An older backup can lack a later terminal stop or last observation**:
never infer current stop status from an old backup alone. An approved real-host
recovery procedure must reconcile external stop/heartbeat evidence and backup
RPO before collection can resume. This is not zero-loss or cloud-recovery proof.

Remaining gates: reviewed evidence-producing adapter, demo timestamp entitlement
and per-level freshness integration, external telemetry/stop reconciliation,
actual host load/reboot/restore tests, exact costs and explicit hosting and
campaign approvals. Collection remains unavailable here. Only after actual new
out-of-sample evidence passes a separate review can an explicitly approved,
isolated automatic paper trial be considered. Live execution stays disabled.
