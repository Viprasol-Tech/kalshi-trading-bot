"""Read-only chronological forecast evaluation; never creates calibration or orders.

One prespecified five-minute-horizon forecast per market. A chronological
development/test split estimates a baseline using only labels already available
before the first test prediction. No random split, refit, or automatic promotion.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from kalshi_bot.btc_model import MODEL_ID
from kalshi_bot.btc_rules import VerifiedMarket, utc_timestamp, verify_market, verify_series


def scores(rows: list[dict[str, Any]], baseline: float | None = None) -> dict[str, Any]:
    if not rows:
        return {"independent_markets": 0, "brier": None, "log_loss": None, "ece": None}
    n = len(rows)
    pairs = [(r["probability"], r["outcome"]) for r in rows]
    bins = []
    for index in range(5):
        group = [(p, y) for p, y in pairs if min(4, int(p * 5)) == index]
        if group:
            mean_p = sum(p for p, _ in group) / len(group)
            mean_y = sum(y for _, y in group) / len(group)
            bins.append(
                {
                    "bin": index,
                    "n": len(group),
                    "mean_prediction": mean_p,
                    "yes_rate": mean_y,
                    "absolute_error": abs(mean_p - mean_y),
                }
            )
    return {
        "independent_markets": n,
        "brier": sum((p - y) ** 2 for p, y in pairs) / n,
        "log_loss": -sum(
            y * math.log(max(1e-12, p)) + (1 - y) * math.log(max(1e-12, 1 - p)) for p, y in pairs
        )
        / n,
        "ece": sum(b["n"] * b["absolute_error"] for b in bins) / n,
        "calibration_bins": bins,
        "baseline_probability_from_prior_labels": baseline,
        "baseline_brier": sum((baseline - y) ** 2 for _, y in pairs) / n
        if baseline is not None
        else None,
    }


def edge(probability: float, ask: Any) -> float | None:
    if not isinstance(ask, (int, float)) or not math.isfinite(ask) or not 0 < ask < 1:
        return None
    # Illustrative one-contract taker fee, M=1; ceil to the next cent.
    fee = math.ceil(0.07 * ask * (1 - ask) * 100 - 1e-12) / 100
    return probability - ask - fee - 0.01


def evaluate(path: Path, *, as_of: float | None = None) -> dict[str, Any]:
    as_of = time.time() if as_of is None else as_of
    if not math.isfinite(as_of) or as_of <= 0:
        raise ValueError("Invalid evaluation cutoff.")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        # Raw series evidence must match the same public demo contract family.
        series_rows = db.execute(
            "SELECT received,body FROM requests WHERE path=? AND status=200 "
            "AND error IS NULL AND fresh=1 AND received<=? ORDER BY received",
            ("/trade-api/v2/series/KXBTC15M", as_of),
        ).fetchall()
        series_evidence = [(at, json.loads(body).get("series", {})) for at, body in series_rows]
        eligible: list[dict[str, Any]] = []
        excluded: dict[str, str] = {}
        for ticker, metadata, outcome, confirmed, final in db.execute(
            "SELECT ticker,metadata,outcome,confirmed_at,final_metadata FROM markets"
        ):
            try:
                market = VerifiedMarket(**json.loads(metadata))
                if (
                    outcome not in (0, 1)
                    or confirmed is None
                    or not market.closes_at <= confirmed <= as_of
                ):
                    raise ValueError("finalized outcome unavailable by cutoff")
                final_raw = json.loads(final)
                forecasts = []
                for at, payload in db.execute(
                    "SELECT at,payload FROM observations WHERE ticker=? AND at<? ORDER BY at",
                    (ticker, market.closes_at),
                ):
                    p = json.loads(payload)
                    probability = p.get("yes_probability")
                    if (
                        not isinstance(probability, (int, float))
                        or not math.isfinite(probability)
                        or not 0 <= probability <= 1
                        or p.get("model_id") != MODEL_ID
                        or not 270 <= market.closes_at - at <= 330
                        or not p.get("reference_valid")
                        or p.get("entries_enabled") is not False
                    ):
                        continue
                    available_series = [s for received, s in series_evidence if received <= at]
                    if not available_series:
                        continue
                    series = available_series[-1]
                    verify_series(series)
                    if verify_market(p["market_metadata"], series, now=at) != market:
                        continue
                    if verify_market(final_raw, series, now=confirmed, finalized=True) != market:
                        continue
                    if (
                        not market.closes_at
                        <= utc_timestamp(final_raw["settlement_ts"])
                        <= confirmed
                    ):
                        continue
                    settled = final_raw.get("settlement_value_dollars")
                    if final_raw.get("result") != ("yes" if outcome == 1 else "no") or settled != (
                        "1.0000" if outcome == 1 else "0.0000"
                    ):
                        continue
                    forecasts.append((at, p))
                if not forecasts:
                    raise ValueError("no verified forecast in fixed 300±30-second horizon")
                at, chosen = min(
                    forecasts, key=lambda item: (abs(market.closes_at - item[0] - 300), item[0])
                )
                probability = float(chosen["yes_probability"])
                quote_usable = (
                    isinstance(chosen.get("book_received_at"), (int, float))
                    and 0 <= at - chosen["book_received_at"] <= 5
                    and isinstance(chosen.get("book_http_age_s"), (int, float))
                    and -1 <= chosen["book_http_age_s"] <= 5
                    and isinstance(chosen.get("book_latency_ms"), (int, float))
                    and 0 <= chosen["book_latency_ms"] <= 2000
                    and chosen.get("quote_timestamp_status") != "invalid_or_stale"
                )
                eligible.append(
                    {
                        "ticker": ticker,
                        "predicted_at": at,
                        "closes_at": market.closes_at,
                        "confirmed_at": confirmed,
                        "outcome": outcome,
                        "probability": probability,
                        "yes_hypothetical_net_edge": edge(probability, chosen.get("yes_ask"))
                        if quote_usable
                        else None,
                        "no_hypothetical_net_edge": edge(1 - probability, chosen.get("no_ask"))
                        if quote_usable
                        else None,
                        "reference_source": "Coinbase BTC-USD proxy, NOT official settlement",
                    }
                )
            except (ValueError, TypeError, KeyError) as exc:
                excluded[ticker] = str(exc)
        eligible.sort(key=lambda r: (r["predicted_at"], r["ticker"]))
        split = max(1, int(len(eligible) * 2 / 3))
        development, test = eligible[:split], eligible[split:]
        known_prior = [
            r for r in development if test and r["confirmed_at"] < test[0]["predicted_at"]
        ]
        baseline = (sum(r["outcome"] for r in known_prior) + 1) / (len(known_prior) + 2)
        return {
            "as_of": as_of,
            "model_id": MODEL_ID,
            "read_only": True,
            "selection": "one nearest 300-second forecast per market, tolerance ±30s",
            "split": "first floor(2N/3), minimum 1 development; remaining chronological test",
            "independent_markets": len(eligible),
            "excluded": excluded,
            "development_metrics": scores(development),
            "test_metrics": scores(test, baseline if known_prior else None),
            "baseline_prior_resolved_markets": len(known_prior),
            "market_records": eligible,
            "entries_enabled": False,
            "calibration_ready": False,
            "fee_assumption": "M=1, ceil(0.07*ask*(1-ask)*100)/100 plus $0.01 slippage",
            "limitations": [
                "Diagnostic fixed-model evaluation, not a fitted or promoted calibration artifact.",
                "All quotes are demo observations; Coinbase is not the CF BRTI settlement index.",
                "Independent market count, not repeated snapshot count, governs sample adequacy.",
                "Six hours yields roughly 24 contracts, far below the current 200-market gate.",
                "No executed trades, empirical execution model, or profitability conclusion.",
                "Retrospective split is exploratory; reserve future untouched data before tuning.",
            ],
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.database.resolve() == args.output.resolve() or args.output.exists():
        raise ValueError("Evaluation output must be a new file separate from the database.")
    args.output.write_text(json.dumps(evaluate(args.database), indent=2) + "\n")


if __name__ == "__main__":
    main()
