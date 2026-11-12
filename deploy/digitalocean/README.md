# DigitalOcean offline hosting package — NOT DEPLOYED

## Multi-market extension — still unreleased

`multi-market-proposal.json` is a plan, not an activation switch. The new engine
uses an isolated `multi-paper.db`; backup/restore now includes that optional file.
Do not run the legacy BTC paper dispatcher alongside it: the legacy dispatcher
remains sealed and the BTC 400-market supervisor remains observation-only.
The new shared policy caps pending plus held markets at three, one per category
and correlated group, with $2 daily gross losses **including all open risk**.
The new mobile UI is available as a loopback-only `multi-dashboard` command.
No public endpoint or service has been deployed. Its private authenticated
proxy, runtime account permissions, atomic read-only status export if mounting
read-only SQLite storage, and actual host acceptance still need deployment review.
No model registry entry is released; there is no config/environment flag that
makes a category validated. See `docs/multi-market-readiness.md` for limitations.

This continues the existing Python repository. Nothing here buys a VPS, opens a
port, creates provider accounts, installs a system service, starts collection or
activates paper/live orders. Do not run installation or network commands until
their separate approvals. No cloud-init that automatically boots a collector is
included. The existing Docker/Compose files are preserved, not repurposed.

## Suggested machine and scope

Ubuntu 24.04 LTS, DigitalOcean Basic regular 1-vCPU/1-GiB/25-GiB VPS: previously
verified public price $6/month; optional daily provider backups +30% gives $7.80.
Budget $8–$10/month including modest encrypted off-site storage/transfer, before
tax, domain, account credits or overages. This is an estimate, not a quote or
billing cap. A bounded seven-day campaign is preferable to perpetual spend with
a $50 trial bankroll. DNS/public TLS can add domain costs. Confirm the account
quote, allowed hosting jurisdiction, provider policy and data licensing first.

One VPS is not highly available. The 750-MiB aggregate application memory budget
leaves limited space for the OS, VPN/proxy and update jobs. Real 24-hour load,
backup overlap and OOM tests are mandatory; upgrade to 2 GiB ($12 compute base
at the previously verified price) only after separate cost approval.

Installed units deliberately supervise an **idle safety guard and dashboard**,
not a market collector. No unit invokes `btc-observe`, `btc-paper`, `run` or
`--live`. A future approved collector needs its own reviewed service, persisted
terminal quality stops and immutable global deadline. Do not disguise the idle
guard heartbeat as quote coverage or market monitoring.

## Layout, identities and secure configuration

- `/opt/kalshi`: reviewed immutable checkout + virtual environment; root-owned,
  not writable by either runtime identity. Pin the reviewed Git commit. Never
  auto-pull or deploy an unreviewed branch. Install existing Python dependencies
  from `pyproject.toml`; capture the exact resolved packages as private release
  evidence. Current dependency ranges are not a reproducible lock file.
- `/var/lib/kalshi`: durable SQLite/control state, owner `kalshi`, group
  `kalshi-read`, mode 0750. Account/control/observation files and existing WAL/SHM
  files must be group-readable; use 0027 umask for future writers. No world access.
- `/var/backups/kalshi`: private backup staging/cache, owner `kalshi`, mode 0700.
  Budget free space for a **second complete snapshot** plus restic cache/WAL.
- Runtime identity `kalshi`; separate login-disabled `kalshi-view` identity,
  member of `kalshi-read`, with filesystem read-only access. The dashboard cannot
  modify the account, write a kill reset or read backup credentials.
- `/etc/kalshi/credentials`: root-owned 0700. Credential files root-owned 0600:
  `restic-repository`, `restic-password`, `aws-access-key`, `aws-secret-key`.
  The backup unit uses systemd `LoadCredential`; only the backup child receives
  S3 credentials, never the dashboard or guard. Do not put secrets in Git,
  `.env.example`, unit command lines, images, log output or backup source sets.
  Repository URL contains **no embedded authentication**. Use bucket-scoped
  backup credentials and an independently stored encrypted-repository recovery
  password. Use the approved secrets flow, not chat, when credentials are needed.
- Unit environments fix `KALSHI_ENVIRONMENT=demo`, `KALSHI_DRY_RUN=true`. No general
  application environment file is sourced. Legacy `KALSHI_API_KEY_ID` remains
  forbidden. A later demo probe must load independently verified DEMO credentials
  directly into its bounded adapter; it is not given to any current service.
- Provider console MFA and SSH keys only; root/password SSH login disabled.
  Permit SSH only from the approved administrator/VPN. No public port 8765.
  Apply OS/security updates with planned restarts; monitor disk/inodes and clock
  synchronization. Do not log environment dumps or enable WebSocket debug logs.

`hosting-approved` and `offsite-backup-approved` are **operator-installed marker
files**, not supplied by this repository. Their creation only follows separate
user approval. Markers are not a substitute for consent records, validation or
paper-trading approval. They never unlock the sealed entry gate.

The guard's data directory ownership must match the writer identity. Existing
ledger files are copied using SQLite's backup API, never reset/reinitialized.
Before transfer, save its logical digest; verify the exact digest and $50/zero
orders/fills on the host. Do not instantiate `PaperBroker` against the user's
ledger as an inspection or recovery step.

## Process restart and monitoring

After approval only, an administrator installs the included `.service`,
`.timer` and `.slice` files and enables the chosen services. No such command has
been run here. They use non-root identities, read-only application trees,
restricted directories, `NoNewPrivileges`, memory limits and bounded retries.

`Restart=on-failure`, 10-second delay and five failures per two minutes avoid
infinite crash loops. Startup failure limits require an operator to inspect and
explicitly restart; this is safer than silently retrying a terminal quality
stop. Boot-enabled services survive an SSH/iPhone disconnect on a functioning
VPS, but that reliability is **not tested on a provisioned host**.

`hosting guard` writes a 15-second atomic idle heartbeat without account changes.
`hosting monitor` exits nonzero for low free space (<2 GiB or <15%), missing or
disarmed control, stale guard heartbeat (>45 seconds), or stale/missing off-site
backup success (>15 minutes). Monitoring timer runs once/minute after a 15-minute
boot grace. This is infrastructure health, not verification of market freshness.
Unit failures and fixed-code JSON events go to journald. Configure bounded
logs (e.g. 100 MiB total, seven days) on the host; do not retain account JSON in
public logs. An independent external heartbeat/notification destination is
still required before unattended collection. No alert account was connected.

Local diagnostic commands (on the **approved, prepared** host):

```sh
/opt/kalshi/.venv/bin/python -m kalshi_bot.hosting health --data /var/lib/kalshi
/opt/kalshi/.venv/bin/python -m kalshi_bot.hosting monitor \
  --data /var/lib/kalshi --backups /var/backups/kalshi
```

Stop/kill procedure: use `btc-kill --journal /var/lib/kalshi/btc-auto-control.db
--reason "operator stop"` to arm the persistent latch; stop any future collector
unit separately. No reset API exists. Killing is not a liquidation or billing
shutdown. Current services cannot place orders even if the latch is disarmed.
No account orders/positions presently need cancellation.

## SQLite backups and recovery

The five-minute backup timer invokes `offsite_backup.py` only with both approval
markers. It creates per-database-consistent snapshots via SQLite's online backup
API, including committed WAL, then verifies `PRAGMA integrity_check` and SHA-256
manifests. It includes only named databases (`paper`, control, observations,
campaign), never credentials/environment files. Each snapshot is consistent,
but there is no cross-database atomic transaction: recovery must reconcile
intents with authoritative account data before any future execution.

Restic encrypts and deduplicates these complete local snapshots into a separately
approved Cloudflare R2 S3-compatible bucket/repository. Stable relative filenames
help deduplication; actual bandwidth/churn is not yet measured. Repository and
bucket creation, `restic init`, provider retention/immutable version policy and
credential setup are **not** automated here. Confirm a versioned/locked backup
policy independently; backup-writer compromise can otherwise destroy history.

The wrapper never echoes credentials or provider output and marks remote success
only after restic reports success. It deletes its own staging copy only after
successful upload; failed sets remain locally for investigation. No automatic
remote pruning, bucket deletion or evidence cleanup. Failed-set growth and
remote history still cost storage; monitor and obtain a retention decision.
Five-minute scheduling is a target, **not a guaranteed five-minute RPO**. A large
database, timeout or failed job can exceed it. Stop collection if backup age
exceeds the reviewed threshold; that collector integration is not built yet.

Tested local drills:

```sh
python -m kalshi_bot.hosting backup --data PRIVATE_SOURCE_DIR --target NEW_BACKUP_DIR
python -m kalshi_bot.hosting verify --data NEW_BACKUP_DIR
python -m kalshi_bot.hosting restore --data NEW_BACKUP_DIR --target NEW_EMPTY_RESTORE_DIR
```

`restore` refuses **any existing target**, verifies checksums/integrity first,
excludes uncommitted WAL and preserves the sticky kill, manifest and ledger.
It does not deploy/start services or extend a campaign deadline.

After approved host loss:

1. Stop all writers; retain the failed disk and latest logs as evidence.
2. Obtain a clean approved host, reviewed source/dependencies and recovery
   credentials from their independent vault. Do not reuse a damaged live folder.
3. Use restic to retrieve a selected verified backup to a **new** private folder.
   Do not copy only an active `.db` and omit its WAL; do not restore over a
   running account. The SQLite restore command above refuses overwrite.
4. Verify manifest, integrity, ledger digest/control state and original campaign
   start/deadline; keep entries sealed and kill armed. Compare backup time with
   the last external heartbeat; report lost observations, never recreate them.
5. For a future approved collector, recheck its immutable manifest, deduplication,
   clock, single-writer lock and persistent terminal quality stop before resume.
   Do not resume after original deadline. A restart is not a new approval.
6. Prove fresh backup and external monitoring before declaring recovery complete.

Acceptance targets needing a real host drill: process restart <=2 minutes,
clean-instance restore <=30 minutes, demonstrated backup RPO and no duplicate
records/deadline extension. These are test targets, not promises. Nonzero-RPO
observation backup is not sufficient proof for eventual real-order recovery.

## iPhone dashboard (configuration only, no hosted URL)

The existing responsive dashboard remains loopback-only, read-only, with no
trading/activation POST endpoint. Its service now loads control journal safety,
actual $50 account, trades, realized P&L, settled-market win rate and recorded
drawdown. Unknown statistics remain unavailable; idle guard status is explicitly
not market collection. Add an observations argument only after an approved
archive exists; do not pretend a missing file is live data.

The guard atomically exports `dashboard-published.json`. The hosted dashboard
serves that export, **not live SQLite** under a read-only mount: read-only SQLite
connections can still need transient WAL/SHM creation. Exports older than 45
seconds return 503; quote edges older than five seconds are cleared at viewing
time. Missing exports return 503, never fabricated balances. The source SQLite
backup identity can create transient WAL/SHM, but uses query-only database
connections; the separate dashboard identity cannot write the data directory.

Recommended private access: approved Tailscale installation on the VPS and iPhone,
tailnet identity/MFA and ACL permitting **only your device/account** to reach the
dashboard. No public `funnel`. Tailscale Serve provides private HTTPS, forwarding
to Caddy's **loopback-only HTTP port 8787**. Caddy requires basic authentication
and forwards Host as `localhost:8765`. Caddy's admin API and configuration
persistence are disabled. Its separate runtime identity cannot read account
files or backup credentials.

Supply `DASHBOARD_USER` and `DASHBOARD_PASSWORD_HASH` through the root-owned 0600
`/etc/kalshi/dashboard.env` service environment file. Generate/transfer the
password hash outside chat. The proxy additionally requires a separately
approved `dashboard-access-approved` marker; it is absent here.

After approval, the intended private proxy setup is `tailscale serve --bg
--https=443 http://127.0.0.1:8787`. Verify this against the installed Tailscale
version and ACL; never use `funnel`. Do **not** relax Host checks or bind Python
or Caddy to `0.0.0.0`. No Caddy/VPN service, certificate, password or dashboard
URL was created. A Tailscale plan/acceptable-use check remains before purchase
or signup; do not assume free pricing applies to the user's account.
On-host acceptance must verify TLS, iPhone sign-in, unauthenticated rejection,
read-only user permissions and no credential leakage before access is released.

## Demo WebSocket timestamp probe

`demo_quote_ws.py` is a separate read-only adapter, not the sealed legacy
authenticated client. Constant demo-only endpoint, RSA handshake, at most 16
BTC tickers, only `orderbook_delta`, explicit real-time interval/NO denomination,
bounded 1–300-second duration, ping timeouts, and **no redirects**. `stream()`
refuses without a separate explicit approval argument; no system unit invokes
it. It has no arbitrary command/order/account interface or production URL option.

Offline parser tests distinguish optional delta `msg.ts_ms`, deprecated `msg.ts`
and envelope `sending_ts_ms`. Snapshots do not invent per-level event timestamps.
Missing/stale times remain ineligible. Sequence gaps/replays, malformed depth,
negative levels or exchange clock regression invalidate all session books and
require a completely new connection/snapshot. Equal exchange clocks remain
separate deltas. Per-level and whole-book provenance are not interchangeable.

This probe is **not integrated into strategy eligibility**; whole-book freshness
always remains unverified. A real authenticated demo entitlement/timestamp test
and reviewed per-level age model are still required. It does not grant permission
to use a production credential or treat a send timestamp as quote-event time.
The adapter now uses the modern `websockets.asyncio` client, pinned to reviewed
version 17.4. Environment proxy discovery is disabled explicitly. All HTTP 3xx
handshakes are rejected, including same-origin redirects and responses without
Location; the adapter never returns a redirect URI or retries it. Offline tests
exercise the actual modern handshake control path with TCP replaced, as well
as deadline, cancellation and closure behavior. See
`docs/modern-websocket-readiness.md`. No authenticated network acceptance test
has run; demo entitlement and actual timestamp/whole-book freshness proof still
remain before deployment. A version pin is not a security certification.

## Reconciliation and 400-market campaign

`btc_reconcile.py` adds read-only reconciliation of existing paper ledger evidence
into the journal: known client ID, complete order/fill receipts, immutable fill
dedup, reservations/inventory, pending cancellations, exits, authoritative final
market/strike/rules/payout/time, atomic terminal result/risk release and restart
idempotency. Conflicts/missing acknowledgements retain risk and arm the kill.
It never mutates a broker/account or submits anything.

The paper simulator uses whole contracts. Fractional partial fills, exchange
pagination/cancel races, authoritative portfolio balance/position comparison and
live exchange recovery are **not implemented/certified**. Incomplete partial-fill
history fails closed; tests do not count that as support for fractional live
execution. There is no activated automatic buy/sell loop, exit-policy release or
paper trial. Existing one-position/$1/8-point/proposed-$2 controls remain.

`campaign-proposal.json` is deliberately **not runnable**: 400 new eligible finalized
contracts, fixed chronological 100 development/100 selection/200 untouched test,
300±30-second horizon, max seven days and <=six-hour segments. Times/hashes are
null until a separately approved preregistration. No old exposed test reuse.
Keep Kalshi authoritative finalized payouts/labels; Coinbase remains a proxy.
Existing discovery rejects missing/unverified floor strikes.

A global supervised collector, strict stream provenance integration, external
health and data-quality stop integration remain required before collection.
The idle guard is not a substitute. After sufficient independent market data:
report Brier, log loss, fixed-bin ECE, hypothetical net edges after asks/fees/
slippage, data coverage and independent test-market count. No profitability
claim follows from infrastructure tests or frequent snapshots.

The **offline coordinator is now implemented** in `btc_supervisor.py`, with fixed
six-hour windows, global slot deduplication, immutable deadline, atomic registry
updates and persistent cross-window quality stops. See
`docs/offline-campaign-supervisor.md` for the 334-test readiness update. It has
no network driver or launch service, and `run_collection()` always refuses.
Wiring and host acceptance remain separate work/approval gates; this addition
does not turn the idle guard into a collector.

Update: `btc_demo_bridge.py` now integrates that coordinator with existing demo
discovery/cycle/settlement algorithms in one database, still behind an unconditional
collection release gate. See `docs/demo-collector-readiness.md` for current offline
results and the exact approval path. No service here starts it; all paid-host,
feed, campaign and paper-trial approvals remain false.

## Go/no-go

Ready for **offline code review and local infrastructure drills only**. Separate
gates: (1) review/hosted CI, (2) approved host/VPN/backup setup + real recovery/load
tests, (3) separately approved bounded demo probe, (4) immutable approved collection,
(5) untouched out-of-sample validation/review, (6) separately approved isolated
automatic paper trial with complete simulated buys/sells/performance reporting.
Live trading requires another explicit approval and a reviewed implementation;
this package contains no live activation.

References: DigitalOcean pricing, systemd.exec/service resource controls, Caddy
basic authentication/reverse proxy, restic S3 repository/backup documentation,
SQLite backup API and Kalshi orderbook/WebSocket docs. Public prices were last
researched 12 November 2026; account-specific costs and availability unverified.
