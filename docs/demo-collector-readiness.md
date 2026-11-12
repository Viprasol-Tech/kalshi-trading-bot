# Demo collector integration — launch remains sealed

The existing 400-market supervisor now uses the existing Kalshi demo collector.
No new app, account migration, hosting purchase/deployment, API feed connection,
400-market collection or paper/live trading was started.

## Implemented and tested offline

- One campaign SQLite database contains original protocol/deadline, raw HTTP
  receipts/parameters, discovery attempts/rejections, reference observations,
  quarantined candidate books, completed frames, frozen forecasts, finalized
  outcomes, quality history and exactly-once source acknowledgements.
- `DemoBridge` reuses existing `Collector` discovery and cycle algorithms, not
  a new ticker generator or six-hour process authorization. BTC series/rule
  verification, aligned intervals, bounded detail hydration/backoff and
  verified `floor_strike` checks remain required. Missing strikes stay unknown.
- Only unsigned demo GETs and the exact public Coinbase BTC proxy endpoint.
  Final destination, credentials/cookies, methods and redirect checks apply.
  Environment proxies are disabled. No exchange portfolio/order/account routes
  or production URLs. Coinbase remains a proxy, never an official settlement.
- Raw book requests explicitly ask for **ten price levels**, not unlimited depth.
  YES/NO bids and executable complementary asks come from actual resting prices;
  spread is calculated only when both prices exist. Missing prices remain null.
  Fractional depth is retained, never rounded up. A one-contract diagnostic
  requires four contracts at the relevant best level for <=25% participation.
- Actual quote-event timestamps remain mandatory. HTTP Date, metadata update
  time, wall-clock receipt or one WebSocket delta do not establish freshness
  for an entire resting book. Existing strict transport/latency/reference checks,
  five-second freshness and conservative executable ask/fee/slippage checks remain.
- Provider/error/discovery receipts are acknowledged exactly once before another
  GET. A crash cannot silently reset ten consecutive failures, quality history
  or the original deadline. Prepared frames have fingerprints; an exact receipt
  can be recovered, but expired slots are never backfilled. Reference warm-up
  resets on interruption. Unknown/conflicting input stops safely.
- At most four finalized-label polls per cycle, round-robin so a delayed older
  label cannot starve newer markets. Only matching Kalshi finalized identity,
  rules, strike, result, payout and settlement time supplies a label. An old
  source receipt after restart requires a new GET, not a freshened timestamp.
- Accepted large frame JSON is not duplicated in the source acknowledgement.
  Full reference history is frozen only in the prespecified 300±30-second
  forecast window; individual reference receipts remain durable throughout.
  Real-host disk growth and backup-headroom/load tests remain required.

The collector's main run, single-cycle adapter, both legacy capture runners and
legacy BTC strategy polling have sealed collection gates. No env flag or approval
string unlocks them. No collection CLI/service is enabled. A future activation
needs separate reviewed release code plus explicit approvals; reaching sample
quotas does not activate anything.

## Evaluation and execution preparation

The original probability model was **not tuned**, especially not against the
eight already exposed old test markets. The previous 19.24-point calibration
error is not fixed by infrastructure tests or a default probability haircut.

The prospective protocol remains 100 development / 100 chronological selection /
200 untouched test contracts, fixed UTC boundaries and deadline, one fixed-horizon
forecast per market. Selection/test scores remain sealed until their prescribed
boundaries. This targets at least 400 new eligible finalized contracts: theoretical
minimum 100 hours, with additional time/headroom needed for warm-up, missing
evidence and final labels. Distinct contracts are not statistical independence.

The read-only evaluator now links frozen forecast hashes to raw supervisor frames
for coverage and both YES/NO estimated net-edge summaries. It uses rounded-up
executable ask +2c adverse slippage and >=2c fee, <=$1 full-contract loss and >=4
displayed contracts. Missing/mismatched evidence is excluded, not guessed. No
hindsight best-tick selection, calibration promotion or profitability claim.
These uncalibrated edges are **not confidence bounds**.

Current real prospective sample: **0 acquired**, not 400 synthetic samples.
No new real Brier score, log loss, calibration result, paper win rate or simulated
profitability exists. Paper win rate/profitability remain null until a separately
approved real paper trial; fixture P&L is never published as strategy performance.

Automatic execution remains behind the sealed entry gate and the armed persistent
kill. Fixed settings: $50 starting paper account, <=$1 full risk per trade,
>=8 percentage-point net edge after costs and empirical calibration protection,
$2 UTC daily gross-loss cap including reserved risk, one unresolved position.
No model artifact, dashboard button or feature/environment flag grants approval.

Additional staged paper machinery:

- A separately created paper-trial ledger can persist an immutable **2c** adverse
  slippage policy for both buying and selling. Existing ledgers cannot silently
  switch policy; the existing $50 account is not repurposed or refunded.
- A proposed, disabled pre-close rule plans one sale during the final 60 seconds
  using a fresh bid, at least four contracts and bid rounded down minus 2c.
  Net sale proceeds cannot be negative after the fee, preserving prepaid risk.
  No automatic invented stop-loss fill; without executable data, await verified
  final settlement. This policy still needs trial-release review.
- Sell dispatch requires a fresh reconciliation snapshot, acknowledged entry,
  exactly one held contract, no pending orders, matching rules and unarmed kill.
  An immutable exit ID/intent is saved before submission; unknown acknowledgement
  kills and retains risk instead of resubmitting.
- Read-only paper reconciliation checks tracked sell acknowledgements/payloads,
  fees/cash/inventory and terminal closure, retaining risk on cancellation/unknown
  state. No exchange fill/cancel API is connected.

**Partial-fill boundary:** the generic simulator's multi-contract partial fills
are tested. The BTC proposal is one whole contract; fractional exchange fills,
exchange cancel races and real exchange reconciliation are NOT supported here.
Unknown/fractional/inconsistent histories fail closed; they are not called fully
implemented live execution. A future live adapter remains separate work.

The observation bridge deliberately never dispatches orders. A reviewed paper
trial orchestrator still must connect accepted quotes to the staged entry/exit/
fill-monitoring adapters and record continuous equity after strategy validation.
These isolated adapter drills are not an operational automatic paper loop.

## Verification and CI

369 local offline tests passed, including mock-HTTP rollover/finalized settlement,
receipt recovery/corruption, timestamp/strike rejection, source timeout logging,
sealed launches and isolated YES/NO entry/exit lifecycle drills. Strict Mypy on
50 source/wrapper files, Ruff lint/format and 87% total statement coverage passed.
Local restic/Caddy/systemd drills ran; deprecation/resource warnings remain.
All network sockets remain forbidden in tests. Execution permission is patched
only inside explicit temporary fixtures, never in runtime code.

Actual hosted testing exposed Windows-only issues in the test event loop and
backup file flushing. The fixes permit only CPython's internal loopback socketpair
by exact code identity (other localhost/external sockets remain blocked), and use
writable handles to flush created backup copies, never the source ledger. Eight
regression tests were added; hosted results are tracked separately from local runs.

The actual account is still $50 with zero orders, fills, fees and realized P&L.
Its logical digest is unchanged:
`8177f821ee9f50524a52380d1217a5cfa7d7ac1626e8367ecc193dd7e9581d1e`.
Original model, six-hour archive and old evaluation fingerprints remain unchanged.

CI is configured for read-only push testing on the existing fork branch as well
as upstream PR testing, without `pull_request_target`, secrets or deployment jobs.
The fork has Actions enabled and the connected account has fork administration.
Upstream fork-run approval remains a separate maintainer control; this account
does not have upstream admin/maintain/push permission. The PR's latest run links
and final chat report distinguish actual hosted results from local passes.

## Exact next steps from an iPhone

1. Open the existing draft PR and its actual CI links. Resolve failures and obtain
   an upstream maintainer's run approval where required; do not confuse a local
   pass or `action_required` with hosted tests having run.
2. **Separately approve a bounded, read-only demo-feed preflight**, not paper
   trading or a 400-market campaign. A reviewed preflight release must verify
   real BTC demo availability, strikes, liquidity and authoritative executable
   quote timestamps. If REST lacks them, the read-only demo WebSocket investigation
   must establish per-level/book freshness; receipt/send time is not substituted.
   No production credentials are needed or permitted. Any demo secrets go through
   the secrets flow, never chat or committed files.
3. Only after evidence passes, approve exact hosting/cost and a fixed campaign
   manifest. Run disk/load, backup/restore/reboot, external stop/clock/health and
   original-deadline recovery acceptance tests on that host. No paid host exists
   or is assumed ready here. Closing this chat is not an unattended-host guarantee.
4. Launch an explicitly approved observation-only campaign; never paper trade
   while gathering the validation sample. Stop and review chronological results,
   calibration, fees/slippage and independent-contract/regime coverage first.
5. Separately approve a reviewed automatic **paper-only** trial and its exit rule.
   Use a new $50 trial ledger, keep production/live locked and record complete
   equity/marked-inventory history for drawdown. No real-money release follows
   automatically from paper results.

The existing mobile-responsive dashboard remains read-only and now shows campaign
cohort counts and sale-inclusive reconciled win rate. Missing equity history shows
unknown drawdown, not a fabricated zero. For actual iPhone access, the prepared
private Tailscale HTTPS → authenticated loopback Caddy → read-only dashboard route
must be separately deployed/verified after hosting approval. There is **no working
hosted dashboard URL or Start Trading button yet**.
