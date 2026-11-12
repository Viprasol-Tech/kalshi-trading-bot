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
from kalshi_bot.btc_observe import book_fields
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


def executable_edges(chosen: dict[str, Any], at: float) -> dict[str, Any]:
    """Conservative one-contract diagnostics, never an order or a fill.

    Unknown event timestamps cannot establish executable freshness. Require
    actual opposite-side depth, not a market summary ask or last trade.
    """
    result: dict[str, Any] = {
        "strict_yes_net_edge": None,
        "strict_no_net_edge": None,
        "strict_execution_eligible": False,
        "strict_edge_reason": None,
        "strict_fee_reserve_dollars": 0.02,
        "strict_slippage_dollars": 0.02,
    }
    try:
        for key in ("book", "reference", "metadata"):
            received = chosen[key + "_received_at"]
            age = chosen[key + "_http_age_s"]
            latency = chosen[key + "_latency_ms"]
            if (
                any(
                    isinstance(x, bool) or not isinstance(x, (float, int)) or not math.isfinite(x)
                    for x in (received, age, latency)
                )
                or not 0 <= at - received <= 5
                or not -1 <= age <= 5
                or not 0 <= latency <= 2000
            ):
                raise ValueError("Stale, future, missing or invalid transport evidence.")
        if not chosen.get("reference_valid"):
            raise ValueError("Reference invalid.")
        stamp = chosen.get("quote_timestamp")
        field = chosen.get("quote_timestamp_field")
        raw_stamp = chosen.get("quote_timestamp_raw")
        parsed_stamp = (
            utc_timestamp(raw_stamp)
            if isinstance(raw_stamp, str)
            else float(raw_stamp)
            if isinstance(raw_stamp, (float, int)) and not isinstance(raw_stamp, bool)
            else None
        )
        if (
            chosen.get("quote_timestamp_status") != "fresh"
            or field
            not in {
                container + "." + key
                for container in ("root", "orderbook_fp")
                for key in ("quote_timestamp", "quote_ts", "orderbook_timestamp", "orderbook_ts")
            }
            or isinstance(stamp, bool)
            or not isinstance(stamp, (int, float))
            or not math.isfinite(stamp)
            or parsed_stamp != stamp
            or not 0 <= at - stamp <= 5
            or not 0 <= chosen["book_received_at"] - stamp <= 5
        ):
            raise ValueError("Actual quote-event timestamp unavailable, invalid or stale.")
        probability = chosen["yes_probability"]
        if (
            isinstance(probability, bool)
            or not isinstance(probability, (float, int))
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
        ):
            raise ValueError("Invalid diagnostic probability.")
        book = book_fields({"orderbook_fp": chosen["raw_book"]})
        any_side = False
        for side, p in (("yes", probability), ("no", 1 - probability)):
            ask, depth = book[side + "_ask"], book[side + "_ask_depth"]
            if ask is None or depth < 1:
                continue
            if chosen.get(side + "_ask") != ask or chosen.get(side + "_ask_depth") != depth:
                raise ValueError("Stored ask/depth disagrees with raw resting book.")
            fee = max(0.02, math.ceil(0.07 * ask * (1 - ask) * 100 - 1e-12) / 100)
            result["strict_" + side + "_net_edge"] = p - ask - fee - 0.02
            any_side = True
        if not any_side:
            raise ValueError("No resting ask with at least one contract of depth.")
        result["strict_execution_eligible"] = True
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        result["strict_yes_net_edge"] = result["strict_no_net_edge"] = None
        result["strict_edge_reason"] = str(exc)
    return result


def evaluate(
    path: Path, *, as_of: float | None = None, split_at: float | None = None
) -> dict[str, Any]:
    as_of = time.time() if as_of is None else as_of
    if not math.isfinite(as_of) or as_of <= 0:
        raise ValueError("Invalid evaluation cutoff.")
    if split_at is not None and (not math.isfinite(split_at) or not 0 < split_at < as_of):
        raise ValueError("Invalid chronological boundary.")
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
                        **executable_edges(chosen, at),
                    }
                )
            except (ValueError, TypeError, KeyError) as exc:
                excluded[ticker] = str(exc)
        eligible.sort(key=lambda r: (r["predicted_at"], r["ticker"]))
        if split_at is None:
            split = max(1, int(len(eligible) * 2 / 3))
            development, test = eligible[:split], eligible[split:]
        else:
            development = [r for r in eligible if r["predicted_at"] < split_at]
            test = [r for r in eligible if r["predicted_at"] >= split_at]
        known_prior = [
            r for r in development if test and r["confirmed_at"] < test[0]["predicted_at"]
        ]
        baseline = (sum(r["outcome"] for r in known_prior) + 1) / (len(known_prior) + 2)
        return {
            "as_of": as_of,
            "model_id": MODEL_ID,
            "read_only": True,
            "selection": "one nearest 300-second forecast per market, tolerance ±30s",
            "split": (
                "first floor(2N/3), minimum 1 development; remaining chronological test"
                if split_at is None
                else "fixed supplied UTC boundary; does not repartition as labels arrive"
            ),
            "split_at": split_at,
            "prospective_registration_verified": False,
            "independent_markets": len(eligible),
            "excluded": excluded,
            "development_metrics": scores(development),
            "test_metrics": scores(test, baseline if known_prior else None),
            "baseline_prior_resolved_markets": len(known_prior),
            "market_records": eligible,
            "entries_enabled": False,
            "calibration_ready": False,
            "fee_assumption": "M=1, ceil(0.07*ask*(1-ask)*100)/100 plus $0.01 slippage",
            "legacy_edge_status": (
                "Historical transport-fresh diagnostic, NOT proof of quote-event freshness "
                "or executable depth. Retained for comparison, never activation."
            ),
            "strict_edge_assumption": (
                "Actual quote-event age <=5s; book/reference/metadata transport <=5s "
                "and latency <=2s; raw resting depth >=1; rounded M=1 fee with >=$0.02 "
                "reserve plus $0.02 slippage. No fill assumption."
            ),
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
    parser.add_argument(
        "--split-at", type=utc_timestamp, help="Predeclared timezone-aware UTC boundary"
    )
    parser.add_argument(
        "--as-of", type=utc_timestamp, help="Evaluate only labels known by this cutoff"
    )
    args = parser.parse_args()
    if args.database.resolve() == args.output.resolve() or args.output.exists():
        raise ValueError("Evaluation output must be a new file separate from the database.")
    result = evaluate(args.database, as_of=args.as_of, split_at=args.split_at)
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
