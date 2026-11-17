# Offline demo metadata and timestamp adapter — unreleased

Continues existing draft PR6. No new app, model fitting, API request, WebSocket
connection, credential loading, collection, paper/live order, deployment or
hosting purchase was performed in this implementation phase.

## Changes delivered

### Explicit current metadata schema

Kalshi's [Get Market schema](https://docs.kalshi.com/api-reference/market/get-market.md)
defines `latest_expiration_time` as the latest possible time a market can
expire. It marks `expiration_time` deprecated; `expected_expiration_time` is
the time a market is expected to expire, not an administrative upper bound.

The BTC rule verifier now recognizes the current administrative bound
explicitly and retains its source field in `VerifiedMarket`. Legacy-only
records still work. Both administrative fields must agree if both are present.
Null/invalid/missing bounds or expected-expiry-only data are rejected. No raw
response is rewritten, no trading close time is changed and no quote-event
timestamp is invented.

This is not an unconditional alias across arbitrary expiry fields. The original
strict rules, settlement source, time alignment, strike, payout and seven-day
administrative bound remain enforced. Lifecycle is checked before parsing other
fields: determined/closed/disputed/amended contracts cannot become active just
because a schema field is now recognized.

### Catalog and rollover rejection

Both BTC candidate filtering and the generic market feed reject explicit
nonactive status before hydration limits. Generic discovery now checks listed
open/close times **before** its six-candidate detail budget. Twelve expired
listings cannot starve a later valid candidate. Query filters are not evidence
of market status.

Rejected catalog records remain in diagnostic inventory; they never yield a
qualified book or simulated entry. The existing round-robin/pagination and
rollover reseed behavior is retained. This was tested offline, not observed
across a new actual market rollover.

### Hardened read-only WebSocket parser

The existing demo book session now rejects:

- Conflicting `msg.ts_ms` and deprecated `msg.ts` values.
- Invalid or discontinuous wall/monotonic receive clocks.
- A delta whose exchange clock is at or below its replacement snapshot.
- Malformed envelopes, fixed-point types, level arrays or out-of-bound depth.
- Invalid subscription sequences, replay, SID changes, maintenance regressions,
  negative resting depth and crossed books, as before.

Corruption invalidates and clears the entire connection epoch. No in-place
reset, cross-connection merging, silent replay or automatic reconnect occurs.
A new connection requires a new session and replacement snapshots; clocks may
restart lower in a genuinely new epoch. Equal-clock **deltas after** a snapshot
are retained in sequence, not deduplicated.

Only real-time subscriptions (`update_interval_ms=0`, NO-denominated prices)
are supported. Conflated-stream semantics are not claimed or enabled.

### Offline projection, never execution release

`OfflineQuoteAdapter` consumes explicitly supplied decoded messages only. It
has no socket, key, URL selection, wall-clock sampler, persistence side effect,
collector loop or engine dispatch.

It reports original message event/send/receive times separately, best actual
bids, complementary asks/depth only where the opposite bid exists, one-sided
state and per-level timestamp provenance. A delta dates only the level it
changes. Untouched levels remain unknown or stale; zero-quantity changes do
not refresh them. Replacement snapshots reset per-level provenance.

The provider's [Orderbook Updates documentation](https://docs.kalshi.com/websockets/orderbook-updates.md)
defines `sending_ts_ms` as the time the message was queued at the network layer,
and delta `ts_ms` as the time the change was recorded. Snapshot examples/schema
do not establish a whole-book event timestamp. A snapshot clock is a change
completeness watermark, not a Unix event time.

Accordingly **whole_book_freshness_verified=false, execution_eligible=false and
entries_enabled=false** are unconditional in this adapter. `as_execution_book()`
refuses release even if individual levels recently changed. Recognizing a
timestamp is not the same as proving a tradable fresh book.

## Evidence versus synthetic tests

`tests/fixtures/demo_preflight_failures.json` contains selected public fields
from the earlier eight-read preflight, not a complete historical dataset:
closed listings returned by an open query, determined/closed detail states,
missing strike/legacy expiration, empty and NO-only books without event times.
No account data, credentials, raw archive or model outputs were published.

Successful metadata and WebSocket cases are explicitly synthetic offline
fixtures. They are not newly observed active markets or verified provider
messages. The prior preflight evidence remains 8 successful GETs, 0 strict
eligible observations, 0 new finalized validation markets.

## Verification for this implementation

- Local full suite: **539 passed, 0 skipped, 12 warnings, 88% coverage**.
- **53 new offline/synthetic cases** beyond the prior 486-test revision.
- Ruff lint/format (118 files), strict Mypy (55 source/wrapper files), whitespace
  checks passed; local native Caddy/restic/systemd drills ran.
- Deprecated websockets legacy import and SQLite ResourceWarnings remain.
- Current-revision hosted CI is reported separately in the delivered readiness
  report; earlier green runs do not cover these source changes.

No socket/credentialed feed, active-market rollover, Coinbase proxy alignment,
host uptime/reboot/off-site upload or independent category-model validation was
performed. Missing operational checks are not hidden offline passes.

## Account and release status

Original paper account remains $50, zero orders/fills/fees/realized P&L.
Experimental BTC model and six-hour archive/evaluation are untouched. No actual
campaign or multi-market paper database exists. All model releases remain
sealed; no new Brier/log-loss/ECE, hypothetical edge or paper profitability
result exists. Historical eight-market test results were not tuned or reused.

Future limits remain $1 per trade, $2 daily gross loss plus unresolved risk,
three pending/held positions, category/correlation limits, >=8pp estimated net
edge including fees/slippage, persistent kill and duplicate protection.

Because sources changed, previously captured validation manifest fingerprints
are stale and must be regenerated/reviewed before any scheduled campaign.
Neither a fresh hash nor a passing test grants collection/paper/live approval.

## Exact remaining path

1. Review this draft and current-source CI. Upstream workflow approval still
   requires an authorized maintainer.
2. Prepare a separately approved, bounded, read-only demo WebSocket probe if
   required; verified demo-only credentials must use the secrets flow, never
   chat or production credentials. No connection was made here.
3. Establish actual active-market metadata and timestamp/sequence provenance,
   including an agreed conservative whole-book freshness criterion. Do not
   promote send/receive time or one fresh delta into whole-book evidence.
4. Preregister numerical model criteria, immutable chronological schedule,
   current code/model/ledger fingerprints and 100/100/200 cohort protocol.
   Obtain and evaluate the 400 new eligible finalized BTC markets with unseen
   test results; other categories require their own models and independent data.
5. Separately approve and accept reliable hosting, durable disk, off-site
   recovery, clocks/alerts and private authenticated iPhone access.
6. Only after a separate paper-release review may a guarded simulation trial
   be prepared. No automatic activation follows this adapter, a JSON flag,
   a preflight, CI success or a model score.
