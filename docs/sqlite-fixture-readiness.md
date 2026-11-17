# Offline SQLite fixture cleanup — no execution release

Continues existing draft PR6. This change modifies four test files only, plus
this document. Runtime code, strategy, model, collector, quote adapter, risk
policy and all release gates are unchanged.

## Confirmed cause and repair

Allocation tracing identified SQLite connections created by test fixtures in
`test_btc_automation.py`, `test_btc_campaign_validate.py` and
`test_btc_readiness.py` that were not explicitly closed. Static inspection found
the same pattern in `test_btc_collect.py`.

SQLite's connection context manager commits or rolls back a transaction but
does not close the connection. The fixtures now use `contextlib.closing`
outside the existing transaction context, preserving commit/rollback ordering
and closing connections even when a fixture raises. The restore drill closes
both source and destination connections after their transaction contexts exit.
No warning filter was added and no assertion or safety check was weakened.

## Actual offline verification

- Fresh complete suite: **539 passed, zero skipped, one warning**, 88% statement
  coverage (4,730 of 5,388 statements; 658 uncovered).
- Previously the same suite reported 12 warnings. No SQLite ResourceWarning was
  observed in the completed cleanup run.
- Allocation-traced targeted diagnostic before repair: **25 passed**, with
  SQLite warnings including two at forced garbage collection.
- Same targeted diagnostic after repair, including forced garbage collection:
  **25 passed**, no reported warnings. These are a subset, not 25 new cases.
- Ruff lint and format (118 files), strict Mypy (55 source/wrapper files) and
  whitespace checks passed. Local native restic, Caddy and systemd drills ran.
- An initial full allocation-traced diagnostic was intentionally stopped and
  replaced by the targeted diagnostic. It was not a completed test pass.

Current-revision hosted CI is reported separately in the delivered readiness
report. Earlier green runs do not cover these changed test fixtures.

## Remaining warning and operational limits

The one remaining warning is the deprecated `websockets.legacy` interface used
by the read-only demo adapter. Installed websockets 17.4 imported successfully
and passed existing offline adapter tests. This does not certify a real
authenticated connection or compatibility with every permitted future version.
A reviewed modern-client migration or dependency constraint, with preserved
redirect rejection and timeout behavior, remains before host acceptance.

No new Kalshi REST request, WebSocket connection, credential loading, market
collection, order, hosting purchase or deployment occurred in this cleanup.
The prior bounded public preflight remains separate evidence: one verified
active BTC market, missing verified event timestamps and inadequate best-price
depth, zero qualifying observations. That historical ticker must not be reused
as if it were still active.

Original account is still $50 with zero orders and fills. Runtime manifest
verification passed without regeneration because runtime source, policy, model
and logical ledger are unchanged. A manifest is not authorization. Secret
access remains blocked based on its last attempt; credential presence and
provenance have not been independently verified.

All collection, simulated-entry and real-order gates remain sealed. Models
remain unreleased; no new independent finalized validation markets or real-data
paper trades exist. Continuous automatic paper trading remains **not ready**.
Existing feed-freshness, model-validation, upstream-approval, costed-hosting and
separate paper-release requirements still apply.
