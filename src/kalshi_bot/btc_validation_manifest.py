"""Offline BTC-first preregistration. Never initializes a campaign or releases IO.

A source/ledger-bound review artifact is not an approval or model certificate.
Prospective times remain unset until a reviewed schedule is explicitly supplied.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from kalshi_bot.btc_campaign import Protocol
from kalshi_bot.btc_model import MODEL_ID, REFERENCE_SOURCE
from kalshi_bot.btc_observe import ledger_state
from kalshi_bot.btc_supervisor import implementation_sha256
from kalshi_bot.multi_market import POLICY, Category, canonical, digest
from kalshi_bot.safety import require_simulation


def source_files() -> dict[str, str]:
    root = Path(__file__).parent
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }


def build_manifest(ledger: Path) -> dict[str, Any]:
    """Read-only fingerprint capture; missing ledgers fail rather than creating $50."""
    require_simulation()
    if not ledger.is_file() or ledger.is_symlink():
        raise ValueError("Existing non-symlink original paper ledger required.")
    state = ledger_state(ledger)
    if tuple(state["account_cents"]) != (5000, 5000, 0, 0) or state["orders"] or state["fills"]:
        raise ValueError("Expected untouched $50 original account, no orders/fills.")
    files = source_files()
    body: dict[str, Any] = {
        "schema": "btc-first-validation-review-v1",
        "status": "DRAFT_NOT_AUTHORIZATION",
        "environment": "demo",
        "dry_run": True,
        "collection_enabled": False,
        "paper_entries_enabled": False,
        "real_orders_enabled": False,
        "production_credentials_allowed": False,
        "hosting_approved": False,
        "runtime_release_ready": False,
        "source_files": files,
        "source_bundle_sha256": digest(files),
        "supervisor_source_sha256": implementation_sha256(),
        "model_sha256": files["btc_model.py"],
        "ledger_sha256": state["logical_sha256"],
        "original_account_cents": list(state["account_cents"]),
        "risk_policy": dict(POLICY),
        "model_id": MODEL_ID,
        "reference_source": REFERENCE_SOURCE,
        "reference_is_official_settlement": False,
        "protocol": None,
        "sampling": {
            "new_distinct_finalized_markets": 400,
            "development": 100,
            "selection": 100,
            "untouched_test": 200,
            "cohorts": "immutable chronological UTC boundaries; no quota-driven boundary moves",
            "independence": "one fixed-horizon forecast per ticker; audit event/time dependence",
            "horizon_seconds": 300,
            "tolerance_seconds": 30,
            "tie_break": "nearest horizon, earlier timestamp; never probability/outcome",
            "maximum_seconds": 604800,
            "segment_seconds": 21600,
            "deadline_extension_allowed": False,
            "quality_stop_auto_resume_allowed": False,
            "historical_archive_role": "already examined; excluded from new untouched cohorts",
            "holdout_reuse_or_tuning_allowed": False,
        },
        "quote_requirements": {
            "actual_event_timestamp": True,
            "maximum_event_age_seconds": 5,
            "maximum_transport_latency_seconds": 2,
            "maximum_metadata_age_seconds": 60,
            "minimum_each_side_depth": 4,
            "maximum_spread_cents": 10,
            "missing_or_conflicting_timestamp": "reject; never HTTP Date/updated_time substitution",
            "one_sided_books": "record incomplete; never qualify",
            "floor_strike": "positive verified numeric value; never inferred",
        },
        "evaluation": {
            "test_scores_sealed_until_original_deadline": True,
            "selection_scores_sealed_until_test_boundary": True,
            "fit_and_calibrate": "development only; select frozen candidate before final test",
            "freeze_before_test": [
                "model bytes",
                "feature definitions",
                "calibration",
                "cost/exit policy",
                "decision thresholds",
                "data exclusions",
                "dataset/cohort hashes",
            ],
            "metrics": [
                "independent contract/event counts",
                "Brier score",
                "log loss",
                "five-bin ECE",
                "development-only Laplace baseline",
                "data coverage",
                "both-side conservative hypothetical net edge",
                "uncertainty intervals",
            ],
            "test_sample_size_is_not_profitability_proof": True,
            "numerical_model_acceptance_thresholds": None,
            "thresholds_must_be_preregistered_before_selection_or_test_review": True,
            "paper_performance": "separate future trial; fees, slippage, entry/exit/settlement",
            "correlation_adjusted_sample_size": (
                "not yet established; 400 tickers != 400 IID samples"
            ),
        },
        "categories": {
            c.value: {
                "model_released": False,
                "new_independent_finalized_markets": 0,
                "brier": None,
                "log_loss": None,
                "calibration_error": None,
                "hypothetical_edge": None,
                "paper_trade_count": 0,
                "paper_win_rate": None,
                "paper_net_pnl": None,
                "paper_max_drawdown": None,
                "reason": "BTC experimental, not validated"
                if c is Category.CRYPTO
                else "No category-specific probability model or matched historical dataset",
            }
            for c in Category
        },
        "required_external_evidence": [
            "bounded demo metadata/book preflight including actual quote timestamp semantics",
            "verified source/feature history recorded before forecasts and official final labels",
            "immutable dataset hashes and no event overlap across chronological cohorts",
            "prespecified numeric model acceptance criteria and baseline comparison",
            "current-source CI and reconciliation/kill/restart/duplicate-order acceptance",
            "approved host costs, persistent disk, verified off-site restore/reboot/clock/alerts",
            "separate paper release review; this manifest never activates execution",
        ],
    }
    return {**body, "manifest_sha256": digest(body)}


def verify_manifest(manifest: dict[str, Any], ledger: Path) -> None:
    """Detect changed code, ledger, policy, fields or approvals; not provenance proof."""
    expected = build_manifest(ledger)
    protocol = manifest.get("protocol")
    if protocol is not None:
        p = Protocol(**protocol)
        p.validate()
        if (p.source_sha256, p.ledger_sha256, p.model_sha256) != (
            expected["supervisor_source_sha256"],
            expected["ledger_sha256"],
            expected["model_sha256"],
        ):
            raise ValueError("Scheduled protocol fingerprints do not match current runtime.")
        expected["protocol"] = asdict(p)
        expected["manifest_sha256"] = digest(
            {k: v for k, v in expected.items() if k != "manifest_sha256"}
        )
    if canonical(manifest) != canonical(expected):
        raise ValueError("Validation manifest changed or became stale; re-review required.")


def schedule_manifest(
    manifest: dict[str, Any],
    ledger: Path,
    *,
    registered_at: float,
    starts_at: float,
    development_ends_at: float,
    test_starts_at: float,
    deadline: float,
) -> dict[str, Any]:
    """Produce a review-only Protocol compatible with the existing supervisor.

    No collection approval, database, current-time claim or future release is
    created. Once scheduled, times cannot be silently changed on restart.
    """
    verify_manifest(manifest, ledger)
    if manifest["protocol"] is not None:
        raise ValueError("Schedule already frozen; no deadline extension or cohort rewrite.")
    p = Protocol(
        registered_at=registered_at,
        starts_at=starts_at,
        development_ends_at=development_ends_at,
        test_starts_at=test_starts_at,
        deadline=deadline,
        source_sha256=manifest["supervisor_source_sha256"],
        ledger_sha256=manifest["ledger_sha256"],
        model_sha256=manifest["model_sha256"],
    )
    p.validate()
    result: dict[str, Any] = json.loads(canonical(manifest))
    result["protocol"] = asdict(p)
    result["manifest_sha256"] = digest({k: v for k, v in result.items() if k != "manifest_sha256"})
    verify_manifest(result, ledger)
    return result
