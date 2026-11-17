# Modern demo WebSocket transport — offline verified, unreleased

Continues existing draft PR6. No Kalshi REST call, WebSocket connection,
credential access, collection, paper/live order, paid hosting or deployment was
performed in this phase. Only offline synthetic fixtures were exercised.

## Migration and safety boundaries

- Replaced deprecated `websockets.legacy.client.Connect` with
  `websockets.asyncio.client.connect`.
- Pinned the reviewed installed WebSocket dependency to **17.4**. Other
  dependencies are not thereby locked; this is not a security certification
  or a claim to support arbitrary future versions.
- Uses modern `additional_headers`, never the legacy `extra_headers` argument.
- Explicitly disables environment proxy discovery with `proxy=None`; the
  read-only approved probe is direct to its constant demo endpoint.
- Modern redirect processing never returns a URI. All HTTP 300–399 responses
  fail closed, including same-origin redirects and missing Location headers.
  Nonredirect handshake failures remain failures, not retry instructions.
- Preserves the explicit approval and simulation checks before signing or IO,
  demo-only constant URL, single orderbook subscription and no order/account
  command interface.
- Preserves the 1–300-second configured read/open deadline (the separately
  approved probe is 60 seconds), ten-second opening timeout, bounded pings,
  two-second close timeout and one-MiB message limit. Closure is a separate
  bounded cleanup step, not a renewed observation window.
- Uses one context-manager connection, never the client's automatic reconnect
  iterator. Quiet logger remains disabled and nonpropagating.
- Rejects nonobject JSON frames before attempting envelope access. Existing
  sequence, exchange-clock, epoch, quote provenance and book rejection checks
  are unchanged.

## Actual tests

- Full offline suite: **593 passed, zero skipped, zero warnings**, in 35.45s.
- Statement coverage: **88%** (4,754 of 5,393 statements; 639 uncovered).
- **54 new transport cases**; targeted parser/adapter/transport run:
  **120 passed**, no warnings.
- Ruff lint/format passed; strict Mypy passed (55 source/wrapper
  files), as did whitespace checks. Local native Caddy, restic restore and
  systemd syntax drills ran.
- Redirect tests cover 300, 301, 302, 303, 304, 307, 308 and 399 with absent,
  same-origin, cross-origin and HTTPS Locations.
- The installed modern connector's actual handshake control path was exercised
  with TCP replaced by an in-memory stub: exactly one opening/handshake attempt,
  transport abort, no follow-on connection and no keepalive after rejection.
- Stream tests replace transport/signing with synthetic fixtures and check
  subscription scope, arguments, malformed/error messages, deadline, explicit
  consumer stop and cancellation cleanup. No actual credential was loaded.
  A temporary test-only RSA key was generated in memory and never serialized.

Current-revision hosted CI is reported separately in the delivered readiness
report. Earlier green CI cannot be reused for this dependency/source change.
Offline transport stubs are not actual TLS, DNS, proxy, network timeout,
authentication or provider entitlement acceptance tests.

## Account, manifest and remaining gates

Original paper account remains $50, zero orders/fills. BTC model, six-hour
archive/evaluation and all risk/execution gates remain unchanged.

The old runtime-source manifest became stale because the demo adapter source
changed. A regenerated review artifact must be integrity-checked and delivered;
it still contains no approved schedule, model threshold or release consent.
Collection, simulated entries, production credentials and real-money orders
remain disabled. The general authenticated exchange client remains sealed.

The user's bounded demo probe approval does not resolve the secure-secret
permission denial. Credential availability and provenance still require secure
verification before any real demo connection. Revalidate an actually active
ticker at that time; do not reuse a historical preflight ticker as current.

This migration does not date untouched snapshot levels or enable executable
whole-book release. The offline quote adapter still always reports
whole_book_freshness_verified=false, execution_eligible=false and
entries_enabled=false. No new independent finalized market observations,
category-model validation or real-data paper trades were obtained.

Before continuous automatic paper trading: prove actual feed freshness and
liquidity, approve upstream CI, preregister and complete the 100/100/200 BTC
validation, accept separately approved hosting/storage/private iPhone access,
and review a separate guarded paper release. No automatic promotion follows
this migration or passing tests.
