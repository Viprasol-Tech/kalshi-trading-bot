"""Read-only mobile status. Loopback only; never deploys or enables execution."""

from __future__ import annotations

import html
import json
import time
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

from kalshi_bot.btc_dashboard import connect_readonly
from kalshi_bot.multi_market import Category
from kalshi_bot.safety import require_simulation


def snapshot(path: Path, *, now: float | None = None) -> dict[str, Any]:
    require_simulation()
    now = time.time() if now is None else now
    result: dict[str, Any] = {
        "mode": "paper-only / model release blocked",
        "live_enabled": False,
        "bankroll_cents": None,
        "available_cents": None,
        "realized_pnl_cents": None,
        "fees_cents": None,
        "kill_armed": True,
        "kill_verified": False,
        "positions": [],
        "trades": [],
        "orders": [],
        "maximum_drawdown_cents": None,
        "marked_equity_cents": None,
        "health": {"fresh": False, "last_cycle": None},
        "limits": {
            "positions": 3,
            "risk_cents": 100,
            "daily_loss_cents": 200,
            "edge": 0.08,
            "positions_per_category": 1,
            "positions_per_correlated_group": 1,
        },
        "categories": {},
        "signals": [],
        "errors": [],
    }
    for category in Category:
        result["categories"][category.value] = {
            "validated_model": False,
            "real_validation_markets": 0,
            "brier": None,
            "log_loss": None,
            "calibration_error": None,
            "closed_positions": 0,
            "wins": 0,
            "win_rate": None,
            "realized_pnl_cents": None,
            "status": "blocked: category model and validation evidence unavailable",
        }
    try:
        with closing(connect_readonly(path)) as db:
            db.execute("BEGIN")
            result["bankroll_cents"] = db.execute("SELECT cash FROM mm_account").fetchone()[0]
            flows = db.execute(
                "SELECT COALESCE(SUM(CASE action WHEN 'buy' THEN -price-fee "
                "ELSE price-fee END),0) FROM mm_fills"
            ).fetchone()[0]
            payouts = db.execute("SELECT COALESCE(SUM(payout),0) FROM mm_settlements").fetchone()[0]
            if result["bankroll_cents"] != 5000 + flows + payouts:
                raise ValueError("Account does not reconcile.")
            reserved = db.execute(
                "SELECT COALESCE(SUM(risk),0) FROM mm_intents WHERE status='pending'"
            ).fetchone()[0]
            result["available_cents"] = result["bankroll_cents"] - reserved
            control = db.execute("SELECT * FROM mm_control").fetchone()
            result["kill_armed"], result["stop_reason"] = bool(control["killed"]), control["reason"]
            result["kill_verified"] = True
            result["positions"] = [
                dict(r)
                for r in db.execute(
                    "SELECT ticker,category,side,risk,status FROM mm_intents "
                    "WHERE status IN ('pending','held')"
                )
            ]
            result["trades"] = [
                dict(r) for r in db.execute("SELECT * FROM mm_fills ORDER BY at DESC LIMIT 100")
            ]
            result["orders"] = [
                dict(r)
                for r in db.execute("SELECT * FROM mm_orders ORDER BY created DESC LIMIT 100")
            ]
            result["fees_cents"] = db.execute(
                "SELECT COALESCE(SUM(fee),0) FROM mm_fills"
            ).fetchone()[0]
            result["realized_pnl_cents"] = db.execute(
                "SELECT COALESCE(SUM(realized),0) FROM mm_intents WHERE status='closed'"
            ).fetchone()[0]
            last = db.execute("SELECT MAX(at) FROM mm_events WHERE kind='heartbeat'").fetchone()[0]
            result["health"] = {
                "fresh": last is not None and 0 <= now - last <= 15,
                "last_cycle": last,
            }
            result["signals"] = [
                {
                    "at": row["at"],
                    "ticker": row["ticker"],
                    "decision": json.loads(row["payload"]),
                    "historical": not 0 <= now - row["at"] <= 5,
                }
                for row in db.execute(
                    "SELECT * FROM mm_events WHERE kind='rank' ORDER BY id DESC LIMIT 12"
                )
            ]
            for row in db.execute(
                "SELECT category,COUNT(*) AS n,SUM(realized>0) AS wins,SUM(realized) AS pnl "
                "FROM mm_intents WHERE status='closed' GROUP BY category"
            ):
                category = result["categories"][row["category"]]
                category.update(
                    closed_positions=row["n"],
                    wins=row["wins"],
                    win_rate=row["wins"] / row["n"],
                    realized_pnl_cents=row["pnl"],
                )
            equity = db.execute("SELECT at,value FROM mm_equity ORDER BY at").fetchall()
            if equity and last is not None and 0 <= now - equity[-1]["at"] <= 5:
                result["marked_equity_cents"] = equity[-1]["value"]
                # Observed complete-book marks only, not an intratick/continuous claim.
                peak, drawdown = 5000, 0
                for mark in equity:
                    peak = max(peak, mark["value"])
                    drawdown = max(drawdown, peak - mark["value"])
                result["maximum_drawdown_cents"] = drawdown
                result["drawdown_scope"] = "observed complete-book equity points only"
    except Exception as exc:
        # Unknown ledger must not appear as a healthy default-funded account.
        result["errors"].append(type(exc).__name__ + ": status unavailable")
        result["kill_armed"] = True
        result["kill_verified"] = False
        result["health"]["fresh"] = False
        for key in ("bankroll_cents", "available_cents", "realized_pnl_cents", "fees_cents"):
            result[key] = None
    return result


def render(data: dict[str, Any]) -> str:
    # No scripts, enable controls, remote resources or unsafe ticker interpolation.
    def money(value: Any) -> str:
        return "N/A" if value is None else f"{value / 100:.2f}"

    rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{html.escape(str(row['status']))}</td>"
        f"<td>{row['closed_positions']}</td><td>{money(row['realized_pnl_cents'])}</td></tr>"
        for name, row in data["categories"].items()
    )
    cash = "Unknown" if data["available_cents"] is None else f"${data['available_cents'] / 100:.2f}"
    positions = html.escape(json.dumps(data["positions"], indent=2))
    trades = html.escape(json.dumps(data["trades"], indent=2))
    orders = html.escape(json.dumps(data["orders"], indent=2))
    signals = html.escape(json.dumps(data["signals"], indent=2))
    performance = html.escape(
        json.dumps(
            {
                "realized_pnl_cents": data["realized_pnl_cents"],
                "fees_cents": data["fees_cents"],
                "marked_equity_cents": data["marked_equity_cents"],
                "observed_drawdown_cents": data["maximum_drawdown_cents"],
                "category_win_rates": {
                    name: row["win_rate"] for name, row in data["categories"].items()
                },
            },
            indent=2,
        )
    )
    health = "Fresh" if data["health"]["fresh"] else "Stale / not running"
    kill = str(data["kill_armed"]) if data["kill_verified"] else "UNVERIFIED — treat as unsafe"
    errors = html.escape(json.dumps(data["errors"]))
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>Multi-market paper bot</title><style>
body{{font:16px system-ui;max-width:960px;margin:auto;padding:18px;
background:#101924;color:#eef4fa}}
section{{background:#1b2a3b;padding:18px;border-radius:12px;margin:16px 0}}
table{{width:100%;border-collapse:collapse}}
td,th{{padding:9px;text-align:left;border-bottom:1px solid #456}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere}}.scroll{{overflow:auto}}
</style></head><body><h1>Multi-market paper bot</h1>
<p>Simulation only. Real-money orders disabled. Model releases blocked.</p>
<section><h2>Available balance {cash}</h2><p>Kill: {kill} · Health: {health}</p>
<p>Diagnostics: {errors}</p>
<p>At most 3 positions · $1 risk/trade · $2 daily gross-loss budget · 8pp net edge</p></section>
<section class="scroll"><table><tr><th>Category</th><th>Readiness</th><th>Closed</th>
<th>Paper P&amp;L</th></tr>{rows}</table></section>
<section><h2>Positions</h2><pre>{positions}</pre></section>
<section><h2>Orders (latest 100)</h2><pre>{orders}</pre></section>
<section><h2>Performance</h2><pre>{performance}</pre></section>
<section><h2>Probabilities &amp; net-edge diagnostics</h2><pre>{signals}</pre></section>
<section><h2>Fills &amp; fees</h2><pre>{trades}</pre></section>
<p>Not an unattended-host guarantee. No Start Trading button.
Missing marks are unknown, not zero.</p>
</body></html>"""


def serve(path: Path, *, port: int = 8766) -> None:
    require_simulation()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
                self.send_error(421)
                return
            if self.path not in ("/", "/status.json"):
                self.send_error(404)
                return
            data = snapshot(path)
            body = (json.dumps(data) if self.path == "/status.json" else render(data)).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json" if self.path.endswith(".json") else "text/html; charset=utf-8",
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
