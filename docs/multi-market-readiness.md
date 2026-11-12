# Multi-market paper framework — NOT RELEASED

## Decision

The existing repository now contains a six-category simulation/risk framework.
It is **not a validated six-category trading strategy**. No real-market paper
session, 400-market campaign, deployment, paid host or real order was started.
The model registry is empty and its release gate always refuses activation.
Configuration cannot turn a supplied probability or `validated: true` flag into
evidence. Your request authorizes automatic simulation in principle; the missing
models, trustworthy data and validation evidence still prevent safe activation.

The BTC strategy, 100-development/100-selection/200-untouched-test supervisor,
archived observations and original $50 account are preserved. The multi-market
engine must never run alongside the legacy BTC paper dispatcher. That dispatcher
and the original collection release gates remain sealed.

## Implemented and tested offline

- Category routing from provider series metadata, not title guesses. Unknown
  categories, fee schedules, unsupported settlement semantics and missing BTC
  floor strikes do not become eligible signals.
- Persistent catalog pagination, a separate first-page rollover reseed that does
  not lose the full-universe cursor, and round-robin hydration across series.
  Catalog/detail/series identity mismatches are rejected.
- Both YES and NO evaluation against executable complementary asks derived only
  from actual opposing bids. Missing sides remain missing; fractional prices and
  depth are preserved, not rounded into imaginary liquidity.
- Quote-event age at most 5 seconds, transport latency at most 2 seconds,
  refreshed metadata at most 60 seconds old, two-sided spread at most 10 cents,
  and at least four displayed contracts on each side for a one-contract trial.
  HTTP Date, receipt time and market-update time are not quote-event timestamps.
- At least 8 percentage points of conservative estimated net edge after entry
  fees, adverse entry slippage, a worst-case quadratic exit-fee reserve, adverse
  exit slippage and the model's uncertainty haircut.
- Verified supported quadratic fee multiplier, upward fee rounding and minimum
  2-cent fee reserve per fill; 2-cent adverse slippage in each direction.
  Unsupported schedules are rejected, not assumed free.
- Separate future `multi-paper.db` trial: $50 initial cash, at most $1 full
  prepaid risk per trade, three pending/held positions, one per category and one
  per correlation group/event. Crypto, economics, politics and financial events
  also share a conservative macro-risk group.
- The $2 UTC daily gross-loss budget includes all unresolved worst-case risk,
  including carried positions. Gains do not replenish it. Three $1 exposures
  are therefore prohibited even though three smaller positions can be allowed.
- Atomic SQLite intent/order/fill/cash/settlement journaling, immutable trial
  policy, WAL/FULL durability, startup and per-transaction reconciliation,
  later-quote-only fills, immutable quote IDs and unique market/order identities.
- Concurrent-worker duplicate protection, cash-corruption refusal, mid-fill
  transaction rollback, deadline-preserving restart recovery, persistent kill,
  cancellation of unfilled paper orders and retention of held inventory/risk.
- A proposed automatic pre-close exit in the last 60 seconds, only on verified
  executable books. Without safe liquidity, there is no invented exit/stop fill;
  the position remains reserved until an authoritative settlement is verified.
- Finalized Kalshi market results, matching identities/rules/strikes/close times,
  binary settlement values, repeat-safe payouts and conflicting-label rejection.
  Neither Coinbase proxies nor model predictions can settle the ledger.
- Optional `multi-paper.db` included in existing offline backup/restore; a
  recovery drill checks account and persistent shutdown state after restore.
- Read-only, mobile-layout dashboard with positions, available cash, fees,
  realized P&L, category win rates, probability/edge diagnostics, observed
  complete-book equity drawdown and controller health. Unknown values are N/A,
  not invented balances or zero risk. It binds loopback only, rejects unexpected
  Host headers, has no trading controls, and cannot submit orders.

The new simulator deliberately uses one whole contract per intent. It does not
claim exchange-like fractional partial-fill/queue-position simulation. Existing
BTC paper-broker partial-fill tests remain; a realistic shared multi-market
partial-fill extension and any exchange/live reconciliation remain future work.

## Category-level results

These are synthetic **state-machine** drills, not statistical model validation.
Each category passes two YES/NO entry-and-final-settlement cases, one automatic
later-quote exit case and one default-release refusal case.

| Category | Synthetic category cases | New independent finalized validation markets | Model readiness |
|---|---:|---:|---|
| BTC / other cryptocurrency | 4 passed | 0 | BTC experimental model preserved, not released; altcoin models absent |
| Sports | 4 passed | 0 | Category/rule-family probability model absent |
| Economics / Federal Reserve | 4 passed | 0 | Category/rule-family probability model absent |
| Weather / temperature | 4 passed | 0 | Category/rule-family probability model absent |
| Politics / elections | 4 passed | 0 | Category/rule-family probability model absent |
| Financial / stock events | 4 passed | 0 | Category/rule-family probability model absent |

For all six categories: new Brier score, log loss, calibration error, real-data
hypothetical net edges, strategy win rate and simulated profitability are
**unavailable**. No new real-data paper trades were made. Fixture probabilities,
fixture wins and fixture P&L are not empirical performance.

The historical BTC six-hour diagnostic remains historical: 8 independent test
markets, 19.24-percentage-point test calibration error, Brier 0.13181958 and log
loss 0.41595637. Its already-examined test cohort was not tuned on or relabeled
as a fresh untouched validation sample. No profitability claim is justified.

## Bounded public demo probe

Three unsigned, demo-only public GET requests succeeded: one catalog page of
10 open listings, one series detail and one order book. The sampled series was
reported as **Exotics**, outside the requested category map. The sampled book
had no explicit supported quote-event timestamp. No model accepted this data.

This limited sample does **not** prove the six requested categories are absent
from the entire demo universe, or that every demo book lacks timestamps.
It establishes neither BTC/category coverage nor executable-feed readiness.
The raw responses were saved in a separate SQLite probe artifact, not counted
as campaign observations or finalized independent markets. API errors in this
bounded probe: 0. No collection process was left running.

Runtime quality guards stop on API/latency failures, under-80% quote coverage
after 20 checked target-category candidates, or ten minutes without a verified
target quote. Unsupported categories are not counted as broken target quotes.

## Verification and limitations

- Full local regression: **446 passed, 0 skipped, 12 warnings; 87% coverage**.
- Ruff checks and formatting: passed, 111 files checked for formatting.
- Strict mypy: passed, 53 source/wrapper files.
- 77 new synthetic tests: 24 category-labelled lifecycle/release cases and 53
  shared safety/discovery/recovery/dashboard cases.
- Native local Caddy, restic and systemd drills ran with available tools.
  Dashboard HTTP routing was tested with an in-process fake server, not deployed.
- Hosted results must be checked against the **new PR head**, not inherited
  from the previously green fork run for `e8fa9208...`.
- Upstream CI still requires a Viprasol-Tech maintainer's approval. A blocked,
  queued, cancelled or zero-job run is never a passing run.
- Original paper ledger: $50 balance/equity, 0 orders, 0 fills; logical digest
  `8177f821ee9f50524a52380d1217a5cfa7d7ac1626e8367ecc193dd7e9581d1e`.
- No operational multi-market account or campaign DB was initialized. No keys
  were loaded for the public probe. Production credentials/orders remain unused.
- Hosted uptime, private iPhone connectivity, host-level process restart,
  scheduled off-site backups, notifications and recovery on an actual VPS
  are **not verified**. No VPS or public service exists for this extension.

## Exact next implementation/configuration steps

1. Prove market availability and whole-book event-timestamp semantics with a
   broader bounded demo survey and the existing read-only demo WebSocket
   investigation. Never substitute production data/credentials or receipt times.
   Any necessary demo credentials belong in Secrets, not chat or source control.
2. Freeze a BTC-first 400-independent-market evidence manifest against the
   reviewed source revision and data adapters before beginning a campaign.
   The optional backup extension changes the hosting component fingerprint;
   do not reuse an earlier manifest hash or relax fingerprint checks.
3. Implement and validate narrowly scoped category/rule-family models with
   timestamped external evidence: sports event data, vintage macro/Fed data,
   station-specific weather forecasts and official observations, election data,
   and financial event/price data as applicable to the actual settlement rules.
   A broad category is not one interchangeable probability model. Each plugin
   must verify its particular strikes, units, event identity and official sources.
4. Pre-register chronological development/selection/untouched-test protocols and
   acceptance criteria. Keep correlated contracts from the same event/date out
   of different splits; report independent events and uncertainty intervals.
   A pooled 400-market campaign is not 400 independent tests for every category.
5. Review calibrated probability uncertainty, after-cost edge, liquidity,
   realistic fills/exits, daily loss and reconciliation evidence before any
   category's paper release. No config-only bypass is supplied.
6. Separately approve a hosting plan/deployment before creating a private
   authenticated iPhone URL. Complete process restart, time synchronization,
   disk/WAL durability, off-site restore, read-only dashboard access and monitoring
   acceptance on that host. Loopback HTTP success alone is not healthy trading.
7. Release only reviewed model families for a bounded automatic paper trial;
   keep all others blocked. Preserve $50/$1/$2/three-position/8pp limits. Report
   fills, costs, settlements, drawdown and category results without claiming
   profitability. Real-money trading remains disabled.

Useful offline commands: `multi-status`, `multi-dashboard`, `multi-kill`, and
the currently blocked `multi-paper-run`. None buys a host or deploys a service.
There is no public dashboard URL or guarantee of running after closing an iPhone
until an approved, tested hosting service exists.
