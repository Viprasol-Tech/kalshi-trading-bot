"""Synthetic/local-only infrastructure drills. Never a real host launch."""

from __future__ import annotations

import json
import sqlite3
from collections import namedtuple
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest

from kalshi_bot.btc_automation import ExecutionJournal
from kalshi_bot.btc_dashboard import read_published, snapshot
from kalshi_bot.btc_observe import ledger_state
from kalshi_bot.hosting import (
    backup,
    guard_status,
    monitoring_status,
    publish_dashboard,
    restore,
    verify_backup,
    write_health,
)
from kalshi_bot.paper import PaperBroker
from tests.unit.test_btc import NOW


def data_dir(root: Path) -> Path:
    root.mkdir()
    PaperBroker(root / "paper.db").close()
    ExecutionJournal(root / "btc-auto-control.db").close()
    return root


def test_consistent_wal_backup_clean_restore_and_original_unchanged(tmp_path: Path) -> None:
    data = data_dir(tmp_path / "data")
    writer = sqlite3.connect(data / "observations.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY,body TEXT)")
    writer.execute("INSERT INTO observations VALUES(1,'committed WAL data')")
    writer.commit()
    writer.execute("INSERT INTO observations VALUES(2,'uncommitted, must not restore')")
    before = ledger_state(data / "paper.db")
    saved = tmp_path / "backup"
    manifest = backup(data, saved)
    assert set(manifest["files"]) == {"paper.db", "btc-auto-control.db", "observations.db"}
    clean = tmp_path / "restored"
    restore(saved, clean)
    assert ledger_state(clean / "paper.db") == before == ledger_state(data / "paper.db")
    with closing(sqlite3.connect(clean / "observations.db")) as db:
        assert db.execute("SELECT * FROM observations").fetchall() == [(1, "committed WAL data")]
    writer.rollback()
    writer.close()
    assert verify_backup(clean) == manifest
    with pytest.raises(ValueError, match="must not exist"):
        restore(saved, clean)
    with pytest.raises(ValueError, match="must be new"):
        backup(data, saved)


def test_corruption_and_path_traversal_refused_before_restore(tmp_path: Path) -> None:
    data = data_dir(tmp_path / "data")
    saved = tmp_path / "backup"
    backup(data, saved)
    (saved / "paper.db").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="checksum"):
        restore(saved, tmp_path / "restore")
    assert not (tmp_path / "restore").exists()
    manifest = json.loads((saved / "manifest.json").read_text())
    manifest["files"]["../secret"] = {"sha256": "a" * 64, "bytes": 4}
    (saved / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="manifest"):
        verify_backup(saved)


def test_no_account_creation_or_secret_capture(tmp_path: Path) -> None:
    data = tmp_path / "empty"
    data.mkdir()
    (data / "runtime.env").write_text("DUMMY_NOT_A_CREDENTIAL=fixture")
    with pytest.raises(ValueError, match="ledger required"):
        backup(data, tmp_path / "backup")
    assert not (data / "paper.db").exists()
    assert not list(tmp_path.glob(".pending-*"))


def test_crash_before_backup_publication_never_exposes_partial_set(tmp_path: Path) -> None:
    data = data_dir(tmp_path / "data")
    with (
        patch("kalshi_bot.hosting.digest", side_effect=OSError("fixture power loss")),
        pytest.raises(OSError),
    ):
        backup(data, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
    assert not list(tmp_path.glob(".pending-*"))


def test_sticky_kill_survives_backup_restore_and_guard_does_not_collect(tmp_path: Path) -> None:
    data = data_dir(tmp_path / "data")
    j = ExecutionJournal(data / "btc-auto-control.db")
    j.kill("offline operator fixture stop", NOW)
    j.close()
    backup(data, tmp_path / "backup")
    restore(tmp_path / "backup", tmp_path / "restored")
    status = guard_status(tmp_path / "restored", now=NOW)
    assert status["kill_armed"] and not status["collection_running"]
    assert not status["entries_enabled"] and not status["live_enabled"]
    before = ledger_state(data / "paper.db")
    write_health(data)
    page = snapshot(data / "paper.db", journal=data / "btc-auto-control.db")
    assert page["kill_armed"] and page["unresolved_intents"] == 0
    assert page["guard_health"]["fresh"]
    assert not page["guard_health"]["collection_running"]
    assert ledger_state(data / "paper.db") == before


def test_health_fails_closed_without_control(tmp_path: Path) -> None:
    status = guard_status(tmp_path, now=NOW)
    assert status["errors"] and status["kill_armed"] and not status["storage_ok"]
    assert not (tmp_path / "btc-auto-control.db").exists()


def test_monitor_checks_guard_and_remote_backup_not_just_process(tmp_path: Path) -> None:
    data = data_dir(tmp_path / "data")
    backups = tmp_path / "backups"
    backups.mkdir()
    (data / "guard-health.json").write_text(json.dumps({"at": NOW - 1}))
    (backups / "last-success.json").write_text(json.dumps({"at": NOW - 200}))
    usage = namedtuple("usage", "total used free")(25 * 1024**3, 5 * 1024**3, 20 * 1024**3)
    with patch("kalshi_bot.hosting.shutil.disk_usage", return_value=usage):
        status = monitoring_status(data, backups, now=NOW)
        assert status["guard_heartbeat_fresh"] and status["offsite_backup_fresh"]
        assert not status["errors"]
        status = monitoring_status(data, backups, now=NOW + 901)
        assert not status["guard_heartbeat_fresh"] and not status["offsite_backup_fresh"]
        assert len(status["errors"]) == 2


def test_monitor_rejects_future_heartbeat(tmp_path: Path) -> None:
    data = data_dir(tmp_path / "data")
    (data / "guard-health.json").write_text(json.dumps({"at": NOW + 1}))
    assert not monitoring_status(data, tmp_path, now=NOW)["guard_heartbeat_fresh"]


def test_atomic_published_dashboard_readonly_and_stale_rejection(tmp_path: Path) -> None:
    data = data_dir(tmp_path / "data")
    before = ledger_state(data / "paper.db")
    with patch("kalshi_bot.btc_dashboard.time.time", return_value=NOW):
        publish_dashboard(data)
    published = data / "dashboard-published.json"
    payload = read_published(published, now=NOW + 1)
    assert payload["bankroll_cents"] == 5000
    assert payload["trades"] == 0 and payload["kill_armed"]
    with pytest.raises(ValueError, match="stale/unsafe"):
        read_published(published, now=NOW + 46)
    assert ledger_state(data / "paper.db") == before
    payload["entries_enabled"] = True
    published.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="stale/unsafe"):
        read_published(published, now=NOW + 2)


def test_published_old_quote_edges_cleared_even_when_export_is_recent(tmp_path: Path) -> None:
    data = data_dir(tmp_path / "data")
    payload = snapshot(data / "paper.db", now=NOW)
    payload["signals"] = [
        {
            "observed_at": NOW - 6,
            "historical": False,
            "strict_execution_eligible": True,
            "strict_yes_net_edge": 0.2,
            "strict_no_net_edge": 0.3,
        }
    ]
    path = tmp_path / "published.json"
    path.write_text(json.dumps(payload))
    result = read_published(path, now=NOW)
    assert result["signals"][0]["historical"]
    assert result["signals"][0]["strict_yes_net_edge"] is None
    assert not result["signals"][0]["strict_execution_eligible"]


def test_service_templates_are_idle_and_approval_gated() -> None:
    root = Path(__file__).parents[2] / "deploy" / "digitalocean"
    for filename in ("kalshi-guard.service", "kalshi-dashboard.service", "kalshi-backup.service"):
        body = (root / filename).read_text()
        assert "ConditionPathExists=/etc/kalshi/hosting-approved" in body
        assert "KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true" in body
        assert "--live" not in body and "btc-paper" not in body
        assert "btc-observe" not in body
        assert "NoNewPrivileges=true" in body
    assert "Restart=on-failure" in (root / "kalshi-guard.service").read_text()
    assert "ReadOnlyPaths=/var/lib/kalshi" in (root / "kalshi-dashboard.service").read_text()
    proposal = json.loads((root / "campaign-proposal.json").read_text())
    assert proposal["new_eligible_finalized_markets_target"] == 400
    assert proposal["untouched_test_minimum"] == 200
    assert not proposal["collection_approved"] and not proposal["entries_enabled"]
    assert proposal["original_deadline"] is None
