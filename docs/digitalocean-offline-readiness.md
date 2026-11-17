# DigitalOcean offline readiness report

12 November 2026. Existing `Viprasol-Tech/kalshi-trading-bot` repository, same
simulation-only draft PR. **Offline configuration prepared; not deployed and not
ready to start a campaign or automatic trading.**

## Delivered

- Ubuntu VPS systemd configurations for an idle safety guard, separate read-only
  dashboard, private authenticated proxy, local monitoring and encrypted backups.
  Automatic bounded restart, non-root identities, restrictive filesystems,
  network isolation and a 750-MiB application slice are specified. Runtime paths
  and approval markers are explicit. No script purchases or boots a server.
- Persistent SQLite layout and online-backup snapshots include committed WAL.
  SHA-256 manifests and integrity checks precede a clean-directory-only restore.
  Existing databases cannot be overwritten/reset by the restore command.
  Snapshot creation and dashboard publication use atomic filesystem operations.
- Off-site restic/R2 wrapper with systemd credential injection, fixed credential-
  free repository format, encrypted deduplication, suppressed provider output,
  upload-success watermark, retained failed snapshots and no automatic pruning.
  No bucket/repository was created and no external upload occurred.
- Monitoring catches missing/stale guard and backup watermarks, low disk and an
  unexpectedly disarmed kill. Idle heartbeat is explicitly not market coverage.
  Persistent kill remains initially armed; there is no reset or trading endpoint.
- Separate bounded read-only Kalshi DEMO WebSocket probe, no arbitrary URL,
  redirects, order commands, account subscriptions or default connection.
  Explicitly separate approval required. Parser distinguishes actual optional
  delta event time, deprecated timestamp and network send time; snapshot level
  ages remain unknown. Missing/stale times are not fabricated. Gaps/replays or
  corrupt/negative/crossed books invalidate the entire session for resnapshot.
- Read-only paper-ledger reconciliation into the separate execution journal:
  client identity, account cash/fees/P&L checks, order/fill/inventory consistency,
  pending cancellation, exit and verified final settlement; immutable fill
  receipts, duplicate prevention, retained unresolved risk, atomic terminal
  results and recovery after a synthetic interrupted commit. No broker/account
  mutation or order placement by this component.
- Future 400-new-market draft configuration: chronological 100 development,
  100 selection, 200 untouched test; fixed 300±30-second forecasts; maximum seven
  days; no deadline extension. All approvals false, dates/hashes intentionally
  absent until preregistration. Existing strict discovery/strike checks preserved.
- iPhone access configuration: private Tailscale HTTPS -> loopback Caddy basic
  authentication -> loopback dashboard. No public Python listener, Caddy admin
  endpoint, public Funnel or account-changing HTTP operations. The hosted
  dashboard serves a writer-side atomic export, not live SQLite under a read-only
  mount. Stale/unsafe exports return 503; old quote edges are cleared.

Full configuration and operational/recovery instructions:
`deploy/digitalocean/README.md`.

## Verification performed here

**298 offline tests passed** on Linux/Python 3.13, including all previous tests.
Ruff lint/format, strict Mypy on 48 files including the backup wrapper, and Git
whitespace checks passed. Socket connections are forbidden by the test suite.
Three optional local tool drills were run, not skipped:

1. **restic 0.18.0:** created a temporary encrypted local filesystem repository,
   backed up consistent snapshots, checked stored data, restored and verified the
   original logical account digest. No S3/R2 request or provider credential used.
2. **Caddy 2.10.0:** validated the actual proxy template with ephemeral local
   fixture authentication. No HTTP server/listener or VPN service was started.
3. **systemd-analyze 253.6:** verified the actual units/timers/slice in a synthetic
   filesystem with inert executable placeholders. This checks syntax/dependency
   assembly, not real boot, runtime paths, permissions, cgroups or restart timing.

Other tests cover committed versus uncommitted WAL, corrupt manifests, traversal,
partial-publication faults, refused overwrite/account creation, persistent kill
after restore, backup upload failure/success accounting with a mocked subprocess,
missing acknowledgements, cancelled orders, existing filled positions, sells,
authoritative settlement, conflicting receipts, unapproved partial-fill history,
terminal-commit rollback, repeat reconciliation, dashboard age and safety gates.

Warnings remain: `websockets.legacy` is deprecated, and the coverage-enabled
suite reported SQLite connection ResourceWarnings. They are not test failures,
but should be resolved before production hardening. The read-only demo adapter
retains compatibility with the existing dependency range; a reviewed modern-
client migration/pin and actual demo handshake are required before deployment.
No warning was suppressed and no authenticated connection was attempted.

The actual $50 account was opened read-only for verification:

- Initial and current cash: **5,000 cents**
- Realized P&L and fees: **0 cents**
- Orders and fills: **0**
- Logical SHA-256 unchanged:
  `8177f821ee9f50524a52380d1217a5cfa7d7ac1626e8367ecc193dd7e9581d1e`

Probability model, six-hour archive and original evaluation hashes remain
unchanged. No held-out markets were fitted/reused. Previous test evidence remains
only eight contracts: Brier 0.13182, log loss 0.41596, calibration error 19.24
percentage points. Historical strict executable edges remain unavailable because
all archived quotes lack actual event timestamps. No new observations, model
validation result, simulated trade or profitability claim was generated here.

Hosted GitHub CI is separate: the preceding run was `action_required`, not passed,
and had no executing jobs. New PR checks must be inspected after publication.
Maintainer permission/approval may be needed; do not bypass fork-workflow safety.

## Not verified / remaining launch blockers

- No DigitalOcean host/account-specific quote, purchased storage, firewall,
  cloud permissions, OS updates, clock accuracy, 24/7 uptime, actual memory
  headroom, external alert delivery, iPhone sign-in or restart/load/reboot drill.
- No actual R2 credentials, entitlement, encrypted off-site upload or clean-cloud
  disaster restore. A local encrypted-restic drill is not off-site recovery proof.
  Five-minute backup scheduling is a target, not guaranteed RPO; database growth
  and backup overlap/egress need measurement. OS/provider snapshots alone are not
  SQLite-consistent recovery. Nonzero RPO can lose observation rows; report them.
- No real demo WebSocket entitlement/timestamp completeness or per-level strategy
  integration. A fresh delta never certifies freshness of every resting level.
  The probe cannot enable entries and is not a persistent collector.
- No reviewed global cross-segment campaign supervisor, terminal quality-stop
  integration, independently verified deadline/manifest or enough unseen markets.
  The idle guard deliberately cannot collect data or restart a stopped campaign.
- The current paper broker uses whole contracts. Fractional exchange partial
  fills, real cancel/fill races, API pagination, authoritative portfolio balance/
  position reconciliation and exchange settlement-correction handling are not
  certified. This paper-ledger auditor is not a complete live exchange executor.
- No activated automatic entry/exit loop, exit-policy release or automatic
  paper trial. Explicit release review and user approval are still required.
  Current sealed gates cannot be enabled by environment flags, good metrics,
  approval markers or a calibration artifact.
- Host dependency ranges are not a reproducible, audited production lockfile.
  Capture reviewed exact dependencies/package versions before real hosting.

## Recommendation and approvals

Retain the economical $6/month 1-GiB VPS proposal with optional daily provider
backups ($7.80 combined) and an **estimated $8–$10/month** total before tax,
account-specific charges and possible VPN/domain costs. A seven-day bounded
validation run is preferable to indefinite hosting against a $50 bankroll.
These previous public-price estimates are not a purchase, cost cap, current
account quote or guarantee that 1 GiB is sufficient.

Recommended sequence:

1. Review this patch and obtain passing hosted CI without weakening safety.
2. Separately approve a specific VPS/backup/private-dashboard setup and budget;
   verify quote, restrictions and recoverability before any collection.
3. Separately approve a bounded DEMO-only timestamp probe, using secure credential
   handling and no production fallback. Review feed evidence.
4. Finish/test the global supervisor and strict data-quality integration, freeze
   UTC boundaries/hashes/costs, then request specific 400-market campaign approval.
5. Evaluate the required genuinely new finalized contracts chronologically with
   untouched test markets; report sample counts, uncertainty, Brier/log loss/ECE,
   coverage and hypothetical cost-adjusted edges. Do not infer profitability.
6. Only after validation/release review and a separate explicit approval, run an
   isolated automatic paper trial with buys/sells, conservative fees/slippage,
   exits, daily gross-loss cap, kill/recovery and complete performance reporting.

Keep $50, <=$1 future risk, >=8-point estimated net edge, proposed $2 daily gross
loss cap and maximum one unresolved position. **No hosting, collection, demo
connection, paper entries or live trading was enabled in this phase.**
