"""Read-only, loopback dashboard. No market network access or execution endpoints."""

from __future__ import annotations

import html
import json
import sqlite3
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from kalshi_bot.btc_validate import executable_edges
from kalshi_bot.safety import require_simulation


def connect_readonly(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def snapshot(
    ledger: Path,
    observations: Path | None = None,
    *,
    now: float | None = None,
    journal: Path | None = None,
) -> dict[str, Any]:
    """Never instantiate PaperBroker; missing data is unknown, never a default $50."""
    require_simulation()
    now = time.time() if now is None else now
    reconciled_pnls: list[int] | None = None
    result: dict[str, Any] = {
        "generated_at": now,
        "mode": "observation-only",
        "entries_enabled": False,
        "live_enabled": False,
        "release_gate": "sealed; separate paper approval required",
        "max_future_risk_cents": 100,
        "minimum_net_edge": 0.08,
        "bankroll_cents": None,
        "realized_pnl_cents": None,
        "trades": None,
        "win_rate": None,
        "maximum_drawdown": None,
        "drawdown_scope": "unknown until a verified equity history exists",
        "signals": [],
        "trade_log": [],
        "errors": [],
        "settlement": "Kalshi finalized CF BRTI rules; Coinbase is a proxy only",
        "daily_loss_limit_cents": 200,
        "maximum_unresolved_positions": 1,
        "kill_armed": None,
        "kill_reason": "Control journal not loaded; entry release remains sealed.",
        "unresolved_intents": None,
        "guard_health": None,
        "campaign": None,
        "reconciled_closed_positions": None,
    }
    if journal is not None:
        try:
            with closing(connect_readonly(journal)) as db:
                db.execute("BEGIN")
                row = db.execute("SELECT killed,reason FROM auto_control WHERE id=1").fetchone()
                if row is None:
                    raise ValueError("Missing control.")
                result["kill_armed"], result["kill_reason"] = bool(row[0]), row[1]
                result["unresolved_intents"] = db.execute(
                    "SELECT COUNT(*) FROM auto_intents WHERE status!='reconciled'"
                ).fetchone()[0]
                reconciled_pnls = [r[0] for r in db.execute("SELECT pnl_cents FROM auto_results")]
                result["reconciled_closed_positions"] = len(reconciled_pnls)
            health_path = journal.parent / "guard-health.json"
            if health_path.exists():
                health = json.loads(health_path.read_text())
                result["guard_health"] = {
                    "fresh": 0 <= now - health["at"] <= 45,
                    "service": "idle-safety-guard",
                    "collection_running": False,
                    "storage_ok": health.get("storage_ok") is True,
                }
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            result["errors"].append("Control/guard status unavailable; release remains sealed.")
    try:
        with closing(connect_readonly(ledger)) as db:
            db.execute("BEGIN")
            account = db.execute("SELECT * FROM account WHERE id=1").fetchone()
            if account is None:
                raise ValueError("Account missing.")
            fills = db.execute("SELECT * FROM fills ORDER BY timestamp DESC,id DESC LIMIT 100")
            result["trade_log"] = [dict(r) for r in fills]
            count = db.execute("SELECT COUNT(*) FROM fills").fetchone()[0]
            pending = db.execute(
                "SELECT COALESCE(SUM(reserved),0) FROM orders WHERE status='pending'"
            ).fetchone()[0]
            closed = db.execute(
                "SELECT s.realized_pnl FROM settlements s WHERE EXISTS("
                "SELECT 1 FROM fills f WHERE f.ticker=s.ticker AND f.action='buy')"
            ).fetchall()
            result.update(
                bankroll_cents=account["cash"],
                realized_pnl_cents=account["realized_pnl"],
                trades=count,
                pending_reserved_cents=pending,
                settled_filled_markets=len(closed),
                win_rate=sum(r[0] > 0 for r in closed) / len(closed) if closed else None,
            )
            if reconciled_pnls and sum(reconciled_pnls) == account["realized_pnl"]:
                result["win_rate"] = sum(p > 0 for p in reconciled_pnls) / len(reconciled_pnls)
                result["win_rate_scope"] = "Reconciled closed market positions, including sales."
            # No invented zero drawdown when there have been trades but no equity records.
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            curve = [account["initial_cash"]]
            if "btc_equity" in tables:
                curve += [
                    r[0] for r in db.execute("SELECT equity FROM btc_equity ORDER BY timestamp")
                ]
            if len(curve) > 1 or (count == 0 and account["cash"] == account["initial_cash"]):
                peak, dd = curve[0], 0.0
                for value in curve:
                    peak = max(peak, value)
                    dd = max(dd, (peak - value) / peak)
                result["maximum_drawdown"] = dd
                result["drawdown_scope"] = (
                    "recorded conservative equity history" if len(curve) > 1 else "no fills"
                )
    except (OSError, ValueError, sqlite3.Error) as exc:
        result["errors"].append("Ledger unavailable: " + type(exc).__name__)
    if observations is not None:
        try:
            with closing(connect_readonly(observations)) as db:
                db.execute("BEGIN")
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supervisor_control'"
                ).fetchone():
                    state = dict(db.execute("SELECT * FROM supervisor_control").fetchone())
                    counts = dict(
                        db.execute(
                            "SELECT cohort,COUNT(*) FROM campaign_forecasts "
                            "WHERE outcome IS NOT NULL GROUP BY cohort"
                        )
                    )
                    result["campaign"] = {
                        "state": state["status"],
                        "stop_reason": state["reason"],
                        "finalized_forecasts": {
                            c: counts.get(c, 0) for c in ("development", "selection", "test")
                        },
                        "required": {"development": 100, "selection": 100, "test": 200},
                        "counts_are_not_validation_or_trading_approval": True,
                        "collection_release": "sealed",
                    }
                result["coverage"] = dict(
                    db.execute(
                        "SELECT COUNT(*) recorded, COALESCE(SUM(valid),0) originally_valid, "
                        "COALESCE(SUM(missing),0) missing, COALESCE(SUM(stale),0) stale "
                        "FROM observations"
                    ).fetchone()
                )
                result["market_counts"] = dict(
                    db.execute(
                        "SELECT COUNT(*) verified, SUM(outcome IS NOT NULL) finalized FROM markets"
                    ).fetchone()
                )
                for row in db.execute(
                    "SELECT at,ticker,valid,missing,stale,payload "
                    "FROM observations ORDER BY at DESC,id DESC LIMIT 30"
                ):
                    payload = json.loads(row["payload"])
                    result["signals"].append(
                        {
                            "ticker": row["ticker"],
                            "observed_at": row["at"],
                            "yes_probability": payload.get("yes_probability"),
                            "no_probability": (
                                1 - payload["yes_probability"]
                                if payload.get("yes_probability") is not None
                                else None
                            ),
                            "quote_timestamp_status": payload.get(
                                "quote_timestamp_status", "unknown"
                            ),
                            "quote_timestamp": payload.get("quote_timestamp"),
                            "historical": not 0 <= now - row["at"] <= 5,
                            **executable_edges(payload, now),
                        }
                    )
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            result["errors"].append("Observation archive unavailable: " + type(exc).__name__)
    return result


def render(data: dict[str, Any]) -> str:
    """Self-contained HTML; escape source text. No third-party JS, no controls to trade."""

    def esc(value: Any) -> str:
        return html.escape(str(value))

    def money(value: Any) -> str:
        return "Unavailable" if value is None else f"${value / 100:,.2f}"

    def percent(value: Any) -> str:
        return "Unavailable" if value is None else f"{value * 100:.2f}%"

    cards = (
        ("Current cash bankroll", money(data["bankroll_cents"])),
        ("Realized P&L", money(data["realized_pnl_cents"])),
        ("Fills", data["trades"] if data["trades"] is not None else "Unavailable"),
        ("Win rate · settled filled markets", percent(data["win_rate"])),
        ("Maximum recorded drawdown", percent(data["maximum_drawdown"])),
        ("Risk cap / minimum net edge", "$1 / 8 points"),
    )
    tiles = "".join(
        f"<article><small>{esc(k)}</small><strong>{esc(v)}</strong></article>" for k, v in cards
    )
    rows = ""
    for s in data["signals"]:
        rows += (
            "<tr>"
            + "".join(
                f"<td>{esc(v)}</td>"
                for v in (
                    s["ticker"],
                    s["observed_at"],
                    percent(s["yes_probability"]),
                    percent(s["no_probability"]),
                    percent(s["strict_yes_net_edge"]),
                    percent(s["strict_no_net_edge"]),
                    "ARCHIVED" if s["historical"] else "RECENT · not execution approval",
                    s["strict_edge_reason"] or "diagnostic only; release sealed",
                )
            )
            + "</tr>"
        )
    trades = "".join(
        "<tr>"
        + "".join(
            f"<td>{esc(r[k])}</td>"
            for k in ("timestamp", "ticker", "side", "quantity", "price", "fee")
        )
        + "</tr>"
        for r in data["trade_log"]
    )
    return f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="15"><title>BTC observation control room</title>
<style>
:root{{color-scheme:dark;font-family:system-ui;background:#0b1220;color:#e6edf6}}
body{{margin:auto;max-width:1300px;padding:28px}}h1{{font-size:28px}}
.badge{{color:#ffc66b;border:1px solid #705326;padding:12px;border-radius:8px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));
gap:12px;margin:24px 0}}
article{{background:#152237;border:1px solid #263950;
border-radius:10px;padding:18px}}
small{{color:#a8b8ca;display:block}}strong{{display:block;font-size:25px;margin-top:8px}}
.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;border-bottom:1px solid #263950;padding:12px;vertical-align:top}}
p,footer{{color:#a8b8ca;line-height:1.6}}th{{color:#bcd7ef}}code{{color:#ffc66b}}
</style><header><small>KALSHI · BTC 15 MINUTE · DEMO DATA</small>
<h1>Observation control room</h1><div class="badge">
AUTOMATIC EXECUTION DISABLED · PAPER ENTRIES DISABLED · LIVE ORDERS BLOCKED</div></header>
<section class="cards">{tiles}</section>
<h2>Validation campaign</h2><p>{esc(data.get("campaign") or "No approved campaign started.")}</p>
<h2>Safety status</h2><p>Daily gross-loss cap: $2 · maximum unresolved positions: 1<br>
Persistent kill armed: {esc(data.get("kill_armed"))}<br>
Kill reason: {esc(data.get("kill_reason", "Unavailable"))}<br>
Unresolved intents: {esc(data.get("unresolved_intents"))}<br>
Idle guard (not market collection): {esc(data.get("guard_health"))}</p>
<p>Predictions are experimental. Coinbase BTC-USD is a proxy, not the settlement
index. Net edges require actual fresh quote timestamps and executable depth.
Archived quotes are never displayed as current opportunities. No trade history is fabricated.</p>
<h2>Probability &amp; net-edge diagnostics</h2>
<div class="scroll"><table><thead><tr>
<th>Market</th><th>Observed UTC epoch</th><th>P(YES)</th><th>P(NO)</th>
<th>YES strict edge</th><th>NO strict edge</th><th>Age status</th><th>Blocking reason</th>
</tr></thead><tbody>{rows or '<tr><td colspan="8">No observations loaded.</td></tr>'}
</tbody></table></div><h2>Paper trade log · most recent 100 fills</h2>
<div class="scroll"><table><thead><tr><th>UTC epoch</th><th>Market</th><th>Side</th>
<th>Quantity</th><th>Price ¢</th><th>Fee ¢</th></tr></thead><tbody>
{trades or '<tr><td colspan="6">No fills. Trading remains disabled.</td></tr>'}
</tbody></table></div>
<h2>Data &amp; validation readiness</h2>
<p>Verified/finalized markets: {esc(data.get("market_counts", "Unavailable"))}<br>
Archive coverage: {esc(data.get("coverage", "Unavailable"))}<br>
Drawdown scope: {esc(data["drawdown_scope"])}<br>
Read errors: {esc(data["errors"])}</p>
<footer>Read-only · refreshes every 15 seconds · no exchange calls, order endpoint,
activation button, or automatic calibration promotion.</footer></html>"""


def serve(
    ledger: Path,
    observations: Path | None,
    *,
    port: int = 8765,
    journal: Path | None = None,
    published_status: Path | None = None,
) -> None:
    """Local only. Deploying publicly requires separate access control and approval."""
    require_simulation()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("Host") not in {f"127.0.0.1:{port}", f"localhost:{port}"}:
                self.send_error(403)
                return
            if self.path not in ("/", "/api/status"):
                self.send_error(404)
                return
            try:
                data = (
                    read_published(published_status, now=time.time())
                    if published_status is not None
                    else snapshot(ledger, observations, journal=journal)
                )
            except (OSError, ValueError, KeyError, TypeError):
                self.send_error(503, "Read-only status unavailable or invalid.")
                return
            body = (
                json.dumps(data, allow_nan=False).encode()
                if self.path == "/api/status"
                else render(data).encode()
            )
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json" if self.path == "/api/status" else "text/html; charset=utf-8",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        server.serve_forever()


def read_published(path: Path, *, now: float) -> dict[str, Any]:
    """Serve an atomic writer-side export, not live SQLite under a read-only mount."""
    require_simulation()
    data: dict[str, Any] = json.loads(path.read_text())
    generated = data["generated_at"]
    if (
        data.get("entries_enabled") is not False
        or data.get("live_enabled") is not False
        or not isinstance(generated, (int, float))
        or isinstance(generated, bool)
        or not 0 <= now - generated <= 45
    ):
        raise ValueError("Published status stale/unsafe.")
    for signal in data["signals"]:
        if not 0 <= now - signal["observed_at"] <= 5:
            signal.update(
                historical=True,
                strict_execution_eligible=False,
                strict_yes_net_edge=None,
                strict_no_net_edge=None,
                strict_edge_reason="Published quote is archived/stale; not executable.",
            )
    return data
