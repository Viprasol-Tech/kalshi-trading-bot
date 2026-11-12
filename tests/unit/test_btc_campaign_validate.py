"""Offline prospective scoring, no feeds, broker, fitting or live account."""

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from kalshi_bot.btc_campaign import CampaignRegistry
from kalshi_bot.btc_campaign_validate import evaluate_campaign
from tests.unit.test_btc import NOW
from tests.unit.test_btc_campaign import protocol


def staged_campaign(path: Path) -> None:
    p = protocol()
    r = CampaignRegistry(path, p)
    try:
        for suffix, at, probability, outcome in (
            ("D", NOW + 100, 0.7, 1),
            ("S", NOW + 10100, 0.4, 0),
            ("T", NOW + 20100, 0.8, 1),
        ):
            ticker = "KXBTC15M-" + suffix + "-15"
            r.forecast(ticker, at, at + 300, probability, "d" * 64)
            r.outcome(ticker, outcome, at + 330, "e" * 64)
    finally:
        r.close()


def test_no_early_test_scores_or_automatic_promotion(tmp_path: Path) -> None:
    path = tmp_path / "campaign.db"
    staged_campaign(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = evaluate_campaign(path, as_of=NOW + 25000)
    assert report["metrics"]["test"]["sealed"]
    assert report["metrics"]["test"]["scores"] is None
    assert report["metrics"]["selection"]["scores"]["brier"] == pytest.approx(0.16)
    assert not report["minimum_sample_counts_met"]
    for key in (
        "entries_enabled",
        "collection_started",
        "model_performance_validated",
        "calibration_ready",
        "data_provenance_verified",
        "protocol_authorization_verified",
    ):
        assert report[key] is False
    assert report["strict_hypothetical_net_edges"] is None
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_fixed_deadline_unseals_exact_scores_and_prior_only_baseline(tmp_path: Path) -> None:
    path = tmp_path / "campaign.db"
    staged_campaign(path)
    report = evaluate_campaign(path, as_of=protocol().deadline)
    test = report["metrics"]["test"]
    assert not test["sealed"]
    assert test["score_eligible_contracts"] == 1
    assert test["scores"]["brier"] == pytest.approx(0.04)
    assert test["scores"]["log_loss"] == pytest.approx(-math.log(0.8))
    assert test["scores"]["ece"] == pytest.approx(0.2)
    assert test["scores"]["baseline_probability_from_prior_labels"] == pytest.approx(2 / 3)
    assert not report["calibration_ready"]


def test_asof_filters_future_labels_and_seals_selection(tmp_path: Path) -> None:
    path = tmp_path / "campaign.db"
    staged_campaign(path)
    report = evaluate_campaign(path, as_of=NOW + 10200)
    assert report["coverage"]["selection"]["pending_at_cutoff"] == 1
    assert report["coverage"]["test"]["forecasts"] == 0
    assert report["metrics"]["selection"]["sealed"]
    assert report["metrics"]["selection"]["scores"] is None


def test_late_pretest_label_excluded_not_used_in_baseline(tmp_path: Path) -> None:
    path = tmp_path / "campaign.db"
    staged_campaign(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "UPDATE campaign_forecasts SET confirmed_at=? WHERE cohort='development'",
            (protocol().test_starts_at + 1,),
        )
    report = evaluate_campaign(path, as_of=protocol().deadline)
    assert report["pretest_labels_unavailable_or_late"] == 1
    assert report["metrics"]["development"]["score_eligible_contracts"] == 0
    assert report["metrics"]["test"]["scores"]["baseline_brier"] is None


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE campaign_forecasts SET probability=2",
        "UPDATE campaign_forecasts SET cohort='development' WHERE cohort='test'",
        "UPDATE campaign_forecasts SET outcome=2",
        "UPDATE campaign_forecasts SET evidence_sha256='unknown'",
        "UPDATE campaign_forecasts SET confirmed_at=0",
        "UPDATE campaign_manifest SET sha256='corrupt'",
    ],
)
def test_tampered_staging_evidence_fails_closed(tmp_path: Path, statement: str) -> None:
    path = tmp_path / "campaign.db"
    staged_campaign(path)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(statement)
    with pytest.raises(ValueError):
        evaluate_campaign(path, as_of=protocol().deadline)


@pytest.mark.parametrize("cutoff", [True, float("nan"), float("inf"), -1])
def test_invalid_cutoffs_rejected(tmp_path: Path, cutoff: float) -> None:
    with pytest.raises(ValueError, match="cutoff"):
        evaluate_campaign(tmp_path / "absent.db", as_of=cutoff)


def test_missing_input_never_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        evaluate_campaign(path)
    assert not path.exists()


def test_manifest_hash_verified_but_not_claimed_external_attestation(tmp_path: Path) -> None:
    path = tmp_path / "campaign.db"
    staged_campaign(path)
    report = evaluate_campaign(path, as_of=protocol().deadline)
    assert report["protocol_sha256"] == hashlib.sha256(protocol().canonical().encode()).hexdigest()
    assert report["cutoff_is_not_wall_clock_attestation"]
    assert json.loads(json.dumps(report, allow_nan=False))["entries_enabled"] is False
