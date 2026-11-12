# BTC observation reliability and six-hour readiness

> Historical pre-run report. The approved six-hour window subsequently completed
> with one recovered host interruption. See
> [post-run validation readiness](validation-readiness-20261112.md) for current
> findings and blockers. No further collection or deployment is authorized.

## Decision

The observation-only collector is ready for a bounded, guarded six-hour run,
subject to user approval. **The six-hour run has NOT started.** This is an
operational readiness result, not a promise of uninterrupted exchange data or
proof of model calibration. Upstream strike publication delays cannot be fixed
by inventing data.

The existing repository and simulation-only draft PR are retained. No new
application, merge, deployment, order, calibration promotion, or live mode.
`KALSHI_ENVIRONMENT=demo` and `KALSHI_DRY_RUN=true` remain mandatory.

## Investigation of all 60 previous discovery gaps

The earlier 30-minute SQLite archive recorded two contiguous rollover windows:

| UTC window on 11 November 2026 | Failed sampling opportunities |
|---|---:|
| 23:30:00.999 through 23:31:17.062 | 39 |
| 23:45:00.285 through 23:45:40.303 | 21 |

The first gap span was about 76 seconds; the second about 40 seconds. These
timestamps are sampling times, not exact exchange outage durations.
Across the two windows, 51 current candidates were rejected for missing
`floor_strike`, and nine list responses still presented a candidate that failed
active-market/time metadata verification. The API also returned an unrelated
old candidate with unsupported rules; rejecting it was correct.

The next contract could say `status=active` while its subtitle remained
`Target price: TBD` and its authoritative strike was absent. List responses
first contained a valid next-market strike at 23:31:19.089 and 23:45:42.336 UTC,
respectively. The old collector only validated list results and retried that
list on every sampling cycle. It did not query candidate details during the
gaps or retain the corresponding candidate books.

The archive does not establish whether the detail endpoint had published a
strike earlier than the list endpoint. Detail hydration is a defensible
improvement, not evidence that the 60 historical gaps would disappear.

## Implemented collection fixes

- Discover provider-returned candidates using the BTC series and current
  minimum close time, rather than relying only on an open-status list filter.
  Upcoming candidates may be observed in list evidence, but are never treated
  as active before their verified opening time.
- Shorten discovery to five seconds near rollover, bound the next refresh by
  the current close time, and discard expired markets before each sample.
  Missing active markets trigger bounded 2/4/8/10-second discovery backoff
  (on the sampling cadence), rather than an uncontrolled discovery loop.
- Hydrate a currently open candidate from its public demo detail endpoint if
  list validation fails. Bound pagination and detail attempts, validate the
  detail ticker, and log structured rejections.
- Keep the existing exact CF BRTI rule/source, active status, positive
  two-decimal strike, 900-second aligned open/close interval, payout, and
  administrative-expiry checks. Do not infer a strike from Coinbase, a subtitle,
  another market, or an eventual settlement.
- Quarantine candidate books while metadata remains unverified. They retain
  raw metadata and available quotes with `verified=false`, null model strike,
  and entries disabled. They are not eligible model/trading samples.
- Retain Coinbase observations independently of market verification. Label
  them explicitly as proxies, never official settlement prices.
- Classify two-sided, YES-only, NO-only, empty, and unavailable books separately.
  Preserve fractional depth and sub-cent prices. Missing quotes stay null;
  zero depth is not executable liquidity. Asks are implied only from actual
  complementary bids.
- Preserve explicit quote/book timestamps if supplied, including their field
  and raw value. Do not relabel receipt time, HTTP Date, or market `updated_time`
  as a quote-update timestamp. Unknown quote age remains unknown.

## Persistent storage and interruption recovery

`btc_collect` extends the existing observation archive, not the paper ledger:

- SQLite WAL with FULL synchronization, bounded lock waits and automatic
  checkpointing. Raw requests commit individually; observations use unique
  `(slot,ticker)` keys and each sampling slot is claimed durably before reads.
- Single-writer kernel lock, released on process death. A second process cannot
  collect into the same file concurrently.
- An immutable initial UTC deadline, interval, ledger digest, quality policy,
  and implementation fingerprint. Resume rejects configuration/source/ledger
  mismatch and cannot extend a six-hour authorization.
- SIGINT/SIGTERM checkpoint and exit. A process crash leaves claimed slots
  marked interrupted on resume. Existing slots are never replayed; outages are
  reported as gaps, not backfilled or disguised as continuous data.
- Resume resets reference-model warm-up to avoid using discontinuous history.
  Historical observations and already recorded outcomes remain intact.
- Completed runs and quality/safety stops cannot resume. A restart after an
  ordinary interruption is explicit and must precede the original deadline.
- Disk-space, ledger, integrity, and quality checks fail closed. No external
  service outage can make this module call a broker.

The wall-clock window includes interruption downtime; it is not six hours of
guaranteed usable samples. Runtime files are persistent on this environment's
disk, not a claim of replicated remote backup. A long run requires an active
host; on approval it should use a bounded keep-alive background task plus a
check-in for a hung process. No recurring schedule or automatic restart has
been created.

### Data-quality stop policy

| Condition | Stop threshold |
|---|---|
| No verified active market | More than 180 seconds |
| No healthy data while a verified market is active | More than 60 seconds |
| Unusable completed sampling slots | More than 40% of the latest 150 slots |
| Stale market snapshots | More than 10% over the recent slot window, at least 30 snapshots |
| Consecutive request failures | 10 per provider, independently counted |
| Free disk space | Less than 250 MB |
| Paper ledger change or clock before run start | Immediate safety stop |

At the proposed two-second cadence, 150 slots are approximately five minutes.
A valid one-sided book with verified metadata and a fresh reference remains a
healthy observation, but not a complete two-sided quote; empty books do not
qualify as healthy. HTTP freshness is not proof of exchange-event quote age.
An explicit stale/future/invalid quote timestamp marks the snapshot stale.
Quality stop reasons and slot coverage are persisted.

## Verification evidence

- 197 offline unit/regression tests passed on Linux/Python 3.13.11. Socket access
  is forbidden in the test suite. Tests cover detail hydration, missing strikes,
  quarantine, timestamps, partial/empty books, per-provider failure thresholds,
  quality stops, terminal/expired resume refusal, account preservation,
  finalized labels, and chronological scoring without duplicate-market inflation.
- The six-hour scheduling boundary test used a virtual clock and 10,800 slots,
  with zero network requests. This is a scheduling/persistence test, not six
  hours of actual market observations.
- Unclean process exit recovery verified WAL integrity, automatic lock release,
  preservation of the single recorded observation, and interrupted-slot handling.
- Public observation-only preflight: 12 November 2026,
  00:07:55.504 through 00:09:25.514 UTC. A SIGTERM interruption after about
  14 seconds was followed by an explicit resume. The initial deadline
  00:09:25.504 UTC was identical in both processes.
- Preflight result: 45 distinct slots, 45 complete two-sided observations, one
  verified market, 147 public requests, zero API errors and zero duplicate
  `(slot,ticker)` records. SQLite integrity check returned `ok`.
- All 45 preflight order-book responses lacked explicit quote timestamps; they
  were honestly recorded as unavailable. Receipt/HTTP/reference times remain
  available separately.
- Paper account before/after: initial/current cash 5,000 cents, realized P&L
  zero, fees zero, orders zero, fills zero. Full logical ledger digest matched
  and was independently rechecked.
- Ruff lint, Ruff formatting, strict Mypy over 40 source files, and diff
  whitespace checks passed locally. Remote CI status must be reported
  separately, not assumed passed.

The short public preflight did not cross a rollover or collect a finalized
outcome. Those paths have offline coverage and the previous archive provides
actual finalized demo outcomes; they are not claimed as newly live-tested
multi-rollover behavior. Small subsequent hardening additions (source
fingerprint checks, richer coverage summaries and empty-book health treatment)
were verified offline, not via another long public run.

## Model validation, without strategy activation

`btc_validate` opens an archive read-only. It selects one forecast nearest
five minutes before close, within a fixed 270–330-second horizon, per distinct
market. It verifies contemporaneous contract/series metadata and finalized
matching outcomes received by the evaluation cutoff.

The first two thirds of chronological market forecasts (minimum one market)
are development data; the remainder are test data. There is no random split,
refit, model tuning, or calibration-artifact export. An optional baseline uses
only development labels that had already resolved before the first test
prediction. It reports Brier score, log loss, five-bin calibration/ECE and
independent market counts, with null test metrics when no held-out sample exists.

Hypothetical net edges use the available fresh ask, a one-contract M=1
quadratic taker fee rounded up to a cent, and one cent assumed slippage.
Missing/stale asks do not receive an edge. These assumptions do not establish
real execution costs or profits. The observation log also retains the earlier
conservative two-cent fee reserve diagnostics; the two cost assumptions are
explicitly distinct.

Applying the fixed-horizon evaluator to the earlier 30-minute archive yielded
one eligible finalized market, one development record, and **zero held-out
markets**. Its descriptive development Brier score is approximately 0.00086,
log loss 0.02977 and ECE 0.02933. With N=1 these are a plumbing/diagnostic result,
not reliable calibration evidence. There is no out-of-sample performance claim.

Six hours spans roughly 24 full contracts, potentially fewer usable/resolved
forecasts. That remains far below the existing 200-distinct-market calibration
gate. No profitability, win rate, or expected trading return is claimed.

## Safety and proposed run

The actual paper account stays at $50. Future risk remains capped at $1 per
trade and future minimum estimated net edge remains eight percentage points.
Neither threshold is an instruction to enter: this observer has no broker,
order submission, simulated-entry, or `--live` option. Existing live-trading
guards and uncalibrated-entry blocking remain in place.

After user approval, the bounded command is:

```bash
env -i PATH="$PATH" HOME=/tmp/kalshi-offline-home \
  KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true KALSHI_API_KEY_ID= \
  .venv/bin/python -u -m kalshi_bot.btc_collect \
  --seconds 21600 --interval 2 --output data/btc-observe-6h.db
```

An interrupted process can be explicitly resumed with the same command and
`--resume`, before its original deadline, only if it was not a terminal quality
or safety stop and its source/configuration/account are unchanged. Do not run
both processes concurrently. The old 30-minute archive is not migrated or
overwritten.

At the deadline or earlier quality stop, verify the account, evaluate eligible
finalized labels read-only, deliver the SQLite archive and metrics, and stop.
Unresolved outcomes stay pending; no post-deadline grace collection, automatic
entries, production credentials, or promotion is authorized.
