"""Read-only scoring of a staged prospective campaign, never promotion or collection.

Final-test scores remain sealed until the original global deadline. A registry
contains declared evidence hashes, not independently authenticated feed evidence.
No output from this module is a calibration artifact or trading authorization.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Any

from kalshi_bot.btc_campaign import Protocol
from kalshi_bot.btc_rules import utc_timestamp, verify_market
from kalshi_bot.btc_validate import executable_edges, scores
from kalshi_bot.safety import require_simulation


def digest(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)
    )


def numeric(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def raw_edge_summary(
    rows: list[dict[str, Any]], frames: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """One frozen forecast per market; no best-tick/side/outcome selection or fills."""
    values: dict[str, list[float]] = {"yes": [], "no": []}
    excluded = 0
    for row in rows:
        try:
            evidence = frames[row["evidence_sha256"]]
            encoded = evidence["canonical"]
            if hashlib.sha256(encoded.encode()).hexdigest() != row["evidence_sha256"]:
                raise ValueError("Raw frame fingerprint mismatch.")
            frame = json.loads(encoded)
            if frame["at"] != row["predicted_at"]:
                raise ValueError("Raw forecast time mismatch.")
            payload = frame["payload"]
            market = verify_market(payload["market_metadata"], frame["series"], now=frame["at"])
            if market.ticker != row["ticker"] or market.closes_at != row["closes_at"]:
                raise ValueError("Raw market identity mismatch.")
            strict = executable_edges(
                {**payload, "yes_probability": row["probability"]}, evidence["received"]
            )
            if not strict["strict_execution_eligible"]:
                raise ValueError("Missing executable timestamp/quote evidence.")
            for side, probability in (("yes", row["probability"]), ("no", 1 - row["probability"])):
                if strict["strict_" + side + "_net_edge"] is None:
                    continue
                # Same conservative cent rounding/slippage as the future planner;
                # no fictitious maker rebates or hindsight side selection.
                limit = (
                    int(
                        (Decimal(str(payload[side + "_ask"])) * 100).to_integral_value(
                            rounding=ROUND_CEILING
                        )
                    )
                    + 2
                )
                fee = max(2, math.ceil(0.07 * (limit / 100) * (1 - limit / 100) * 100))
                if not 1 <= limit <= 99 or limit + fee > 100:
                    continue
                if payload[side + "_ask_depth"] < 4:
                    continue
                values[side].append(probability - (limit + fee) / 100)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            excluded += 1
    return {
        "forecast_markets_considered": len(rows),
        "excluded_missing_unverified_evidence": excluded,
        "sides": {
            side: {
                "qualified_quote_count": len(v),
                "mean_estimated_net_edge": sum(v) / len(v) if v else None,
                "maximum_estimated_net_edge": max(v, default=None),
                "at_least_eight_percentage_points": sum(edge >= 0.08 for edge in v),
            }
            for side, v in values.items()
        },
        "assumptions": "One contract; executable ask rounded up +2c slippage; >=2c fee; "
        "<=$1 full loss; >=4 resting contracts; no calibrated probability haircut applied.",
        "not_a_confidence_bound": True,
        "not_a_trade_or_portfolio_simulation": True,
        "entries_enabled": False,
    }


def evaluate_campaign(path: Path, *, as_of: float | None = None) -> dict[str, Any]:
    """Score one forecast per contract with fixed cohorts and label availability.

    ``as_of`` is an explicit offline cutoff, NOT proof of elapsed wall-clock time.
    Self-declared registration/provenance is always marked unverified. No release
    consumer may treat this report as an approval, even after all quotas are met.
    """
    require_simulation()
    cutoff = time.time() if as_of is None else as_of
    if not numeric(cutoff) or cutoff <= 0:
        raise ValueError("Invalid evaluation cutoff.")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")  # Consistent snapshot if the staging writer is running.
        manifest = db.execute(
            "SELECT canonical,sha256 FROM campaign_manifest WHERE id=1"
        ).fetchone()
        if manifest is None:
            raise ValueError("Missing campaign manifest.")
        canonical, declared_hash = manifest
        protocol = Protocol(**json.loads(canonical))
        if (
            protocol.canonical() != canonical
            or hashlib.sha256(canonical.encode()).hexdigest() != declared_hash
        ):
            raise ValueError("Campaign manifest fingerprint mismatch.")
        db.row_factory = sqlite3.Row
        raw = db.execute("SELECT * FROM campaign_forecasts ORDER BY predicted_at,ticker").fetchall()
        frames: dict[str, dict[str, Any]] = {}
        snapshot_coverage = None
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='supervisor_frames'"
        ).fetchone():
            frames = {
                r["sha256"]: dict(r)
                for r in db.execute(
                    "SELECT s.sha256,s.canonical,s.received FROM supervisor_frames s "
                    "JOIN campaign_forecasts f ON s.sha256=f.evidence_sha256 WHERE s.received<=?",
                    (cutoff,),
                )
            }
            covered = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(complete),0),COALESCE(SUM(stale),0) "
                "FROM supervisor_frames WHERE received<=?",
                (cutoff,),
            ).fetchone()
            snapshot_coverage = {
                "recorded": covered[0],
                "complete": covered[1],
                "stale": covered[2],
                "complete_percentage": 100 * covered[1] / covered[0] if covered[0] else None,
                "denominator": "Recorded frames only; supervisor reports outage slots separately.",
            }

    groups: dict[str, list[dict[str, Any]]] = {
        "development": [],
        "selection": [],
        "test": [],
    }
    counts = {
        name: {"forecasts": 0, "finalized_by_cutoff": 0, "pending_at_cutoff": 0} for name in groups
    }
    exclusions: dict[str, str] = {}
    pretest_unavailable = 0
    seen: set[str] = set()
    for row in raw:
        r = dict(row)
        ticker = r["ticker"]
        if not isinstance(ticker, str) or ticker in seen:
            raise ValueError("Invalid or duplicate market identity.")
        seen.add(ticker)
        if not all(numeric(r[key]) for key in ("predicted_at", "closes_at", "probability")):
            raise ValueError("Invalid forecast numeric evidence.")
        cohort = protocol.cohort(r["predicted_at"])
        if (
            not ticker.startswith("KXBTC15M-")
            or cohort != r["cohort"]
            or not 270 <= r["closes_at"] - r["predicted_at"] <= 330
            or not 0 <= r["probability"] <= 1
            or not digest(r["evidence_sha256"])
        ):
            raise ValueError("Forecast conflicts with the frozen protocol.")
        if r["predicted_at"] > cutoff:
            continue
        counts[cohort]["forecasts"] += 1
        if r["outcome"] is not None and (
            type(r["outcome"]) is not int
            or r["outcome"] not in (0, 1)
            or not numeric(r["confirmed_at"])
            or not r["closes_at"] <= r["confirmed_at"] <= protocol.deadline
            or not digest(r["outcome_evidence_sha256"])
        ):
            raise ValueError("Invalid staged finalized outcome evidence.")
        available = r["outcome"] is not None and r["confirmed_at"] <= cutoff
        if available:
            counts[cohort]["finalized_by_cutoff"] += 1
        else:
            counts[cohort]["pending_at_cutoff"] += 1
        if cohort != "test" and (not available or r["confirmed_at"] >= protocol.test_starts_at):
            pretest_unavailable += 1
        if not available:
            exclusions[ticker] = "finalized label unavailable by cutoff"
        elif cohort != "test" and r["confirmed_at"] >= protocol.test_starts_at:
            exclusions[ticker] = "pre-test label not available before test boundary"
        else:
            groups[cohort].append(r)

    minima = {"development": 100, "selection": 100, "test": 200}
    # Fixed development-only Laplace baseline. Never fitted to selection/test.
    test_rows = groups["test"]
    first_test = min((r["predicted_at"] for r in test_rows), default=protocol.test_starts_at)
    prior = [r for r in groups["development"] if r["confirmed_at"] < first_test]
    baseline = (sum(r["outcome"] for r in prior) + 1) / (len(prior) + 2) if prior else None
    metrics = {}
    for cohort, rows in groups.items():
        sealed = (cohort == "test" and cutoff < protocol.deadline) or (
            cohort == "selection" and cutoff < protocol.test_starts_at
        )
        metrics[cohort] = {
            "sealed": sealed,
            "score_eligible_contracts": len(rows),
            "required_contracts": minima[cohort],
            "scores": None if sealed else scores(rows, baseline if cohort == "test" else None),
            "hypothetical_edges": None if sealed else raw_edge_summary(rows, frames),
        }
    return {
        "as_of": cutoff,
        "cutoff_is_not_wall_clock_attestation": True,
        "protocol_sha256": declared_hash,
        "model_sha256": protocol.model_sha256,
        "read_only": True,
        "coverage": counts,
        "excluded_contracts": exclusions,
        "pretest_labels_unavailable_or_late": pretest_unavailable,
        "metrics": metrics,
        "baseline": "Laplace-smoothed development labels available before first test prediction",
        "minimum_sample_counts_met": all(len(groups[c]) >= minima[c] for c in groups),
        "snapshot_data_coverage": snapshot_coverage,
        "strict_hypothetical_net_edges": {c: metrics[c]["hypothetical_edges"] for c in metrics}
        if snapshot_coverage is not None
        else None,
        "paper_trial_completed": False,
        "paper_trade_count": 0,
        "paper_win_rate": None,
        "simulated_profitability": None,
        "protocol_authorization_verified": False,
        "data_provenance_verified": False,
        "model_performance_validated": False,
        "calibration_ready": False,
        "entries_enabled": False,
        "collection_started": False,
        "limitations": [
            "Registry hashes are declarations, not authenticated market/settlement evidence.",
            "Raw edges require matching supervisor frames; missing evidence is excluded.",
            "Estimated edges are not calibrated confidence bounds or simulated profitability.",
            "Distinct contracts are not proof of statistical independence; time-block "
            "uncertainty and regime coverage still required.",
            "No fitting, threshold selection, calibration artifact, promotion or orders.",
            "Sealing follows the supplied cutoff; it is not an external blind-test custodian.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", type=utc_timestamp, help="Explicit offline evaluation cutoff")
    args = parser.parse_args()
    report = evaluate_campaign(args.database, as_of=args.as_of)
    # Exclusive create prevents overwriting evidence or the source database.
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
