"""Prospective registry checks, no network or actual collection."""

from dataclasses import replace
from pathlib import Path

import pytest

from kalshi_bot.btc_campaign import CampaignRegistry, Protocol
from tests.unit.test_btc import NOW


def protocol() -> Protocol:
    return Protocol(
        NOW - 1, NOW, NOW + 10000, NOW + 20000, NOW + 50000, "a" * 64, "b" * 64, "c" * 64
    )


def test_fixed_cohort_membership_dedup_restore_and_original_deadline(tmp_path: Path) -> None:
    path = tmp_path / "campaign.db"
    p = protocol()
    r = CampaignRegistry(path, p)
    r.forecast("KXBTC15M-A-15", NOW + 20100, NOW + 20400, 0.7, "d" * 64)
    r.forecast("KXBTC15M-A-15", NOW + 20100, NOW + 20400, 0.7, "d" * 64)
    r.outcome("KXBTC15M-A-15", 1, NOW + 20430, "e" * 64)
    r.outcome("KXBTC15M-A-15", 1, NOW + 20430, "e" * 64)
    assert r.readiness()["finalized_distinct_markets"] == {
        "development": 0,
        "selection": 0,
        "test": 1,
    }
    assert not r.readiness()["minimum_sample_counts_met"]
    assert not r.readiness()["collection_started"]
    backup = tmp_path / "snapshot.db"
    r.backup(backup)
    with pytest.raises(ValueError, match="new destination"):
        r.backup(backup)
    r.close()
    restored = CampaignRegistry(backup, p)
    assert restored.readiness()["finalized_distinct_markets"]["test"] == 1
    restored.close()
    with pytest.raises(ValueError, match="protocol changed"):
        CampaignRegistry(path, replace(p, deadline=p.deadline + 1))


def test_conflicting_or_leaked_records_rejected(tmp_path: Path) -> None:
    r = CampaignRegistry(tmp_path / "c.db", protocol())
    try:
        r.forecast("KXBTC15M-A-15", NOW + 100, NOW + 400, 0.7, "d" * 64)
        with pytest.raises(ValueError, match="Conflicting"):
            r.forecast("KXBTC15M-A-15", NOW + 100, NOW + 400, 0.8, "d" * 64)
        with pytest.raises(ValueError, match="Conflicting"):
            r.forecast("KXBTC15M-A-15", NOW + 20100, NOW + 20400, 0.7, "d" * 64)
        with pytest.raises(ValueError, match="outside"):
            r.forecast(
                "KXBTC15M-B-15", protocol().deadline, protocol().deadline + 300, 0.7, "d" * 64
            )
        with pytest.raises(ValueError, match="late"):
            r.outcome("KXBTC15M-A-15", 1, protocol().deadline + 1, "e" * 64)
        # Delayed development label is recorded but never hidden as pre-test knowledge.
        r.outcome("KXBTC15M-A-15", 1, protocol().test_starts_at + 1, "e" * 64)
        assert r.readiness()["pretest_label_leakage_or_pending"] == 1
        with pytest.raises(ValueError, match="Conflicting"):
            r.outcome("KXBTC15M-A-15", 0, protocol().test_starts_at + 1, "e" * 64)
    finally:
        r.close()


@pytest.mark.parametrize(
    "change",
    [
        {"collection_approved": True},
        {"paid_hosting_approved": True},
        {"entries_enabled": True},
        {"test_minimum": 8},
        {"model_sha256": "unknown"},
        {"test_starts_at": NOW - 1},
        {"deadline": NOW + 604801},
        {"model_id": "fitted-on-test"},
    ],
)
def test_protocol_cannot_grant_permissions_or_lower_requirements(change: dict) -> None:
    with pytest.raises(ValueError):
        replace(protocol(), **change).validate()
