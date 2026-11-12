"""Release boundary tests never bypass the real entry lock.

Pure staging tests may disarm ONLY an isolated temporary journal latch with SQL.
No fixture grants actual dispatch or broker permission.
"""

from __future__ import annotations

import copy
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from kalshi_bot.btc_automation import ExecutionJournal, Opportunity, opportunity
from kalshi_bot.btc_dashboard import render, snapshot
from kalshi_bot.btc_model import Estimate
from kalshi_bot.btc_observe import ledger_state
from kalshi_bot.cli import app
from kalshi_bot.exchange.models import Action, OrderRequest, Side
from kalshi_bot.paper import PaperBroker
from kalshi_bot.safety import SimulationOnlyError, require_entry_release
from tests.unit.test_btc import MARKET_DATA, NOW, SERIES_DATA, history, paper_quote
from tests.unit.test_btc_readiness import executable_payload


def payload() -> dict:
    return copy.deepcopy({**executable_payload(), "market_metadata": MARKET_DATA})


def staged(ticker: str = MARKET_DATA["ticker"], now: float = NOW) -> Opportunity:
    return Opportunity(ticker, now, "a" * 64, 0.8, 0.2, -0.3, "yes", 47, 1, 49, ())


def disarm_fixture_only(journal: ExecutionJournal) -> None:
    journal.db.execute("UPDATE auto_control SET killed=0")


@pytest.mark.parametrize("value", ["true", "1", "approved", "false"])
def test_env_flags_cannot_enable_entries(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("KALSHI_AUTOMATIC_EXECUTION_ENABLED", value)
    monkeypatch.setenv("KALSHI_PAPER_TRADING_APPROVED", value)
    with pytest.raises(SimulationOnlyError, match="Entries disabled"):
        require_entry_release()


def test_direct_broker_blocks_orders_fills_and_persistent_backtest_escape(tmp_path: Path) -> None:
    path = tmp_path / "paper.db"
    broker = PaperBroker(path)
    before = ledger_state(path)
    try:
        with pytest.raises(SimulationOnlyError):
            broker.submit(
                OrderRequest(ticker="T", side=Side.YES, action=Action.BUY, count=1, yes_price=40),
                now=NOW,
            )
        with pytest.raises(SimulationOnlyError):
            broker.process_quote(paper_quote(), now=NOW)
        assert ledger_state(path) == before
        with pytest.raises(ValueError, match="isolated"):
            PaperBroker(path, offline_backtest=True)
    finally:
        broker.close()


def test_cli_paper_and_generic_run_block_before_network() -> None:
    runner = CliRunner()
    for args in (["btc-paper"], ["run", "momentum", "T"]):
        r = runner.invoke(app, args)
        assert r.exit_code != 0
        assert isinstance(r.exception, SimulationOnlyError)


@pytest.mark.parametrize("p,side", [(0.9, "yes"), (0.05, "no")])
def test_planner_recomputes_both_sides_and_uses_conservative_costs(p: float, side: str) -> None:
    with (
        patch("kalshi_bot.btc_automation.estimate", return_value=Estimate(p, 0.001, 121, 300)),
        patch(
            "kalshi_bot.btc_automation.calibration_check",
            return_value={"passed": True, "probability_haircut": 0.05},
        ),
    ):
        decision = opportunity(
            payload(), SERIES_DATA, history(), None, now=NOW, available_cents=5000
        )
    assert decision.side == side
    assert decision.yes_probability == p  # caller's .7 was NOT trusted
    assert decision.yes_edge is not None and decision.no_edge is not None
    assert decision.count == 1 and decision.maximum_loss_cents <= 100
    assert decision.limit_cents is not None
    assert decision.maximum_loss_cents == decision.limit_cents + 2
    assert min(decision.yes_edge, decision.no_edge) < 0


@pytest.mark.parametrize(
    "change",
    [
        {"quote_timestamp_status": "unavailable"},
        {"quote_timestamp_raw": NOW - 6, "quote_timestamp": NOW - 6},
        {"reference_valid": False},
        {"book_received_at": NOW - 8},
        {"yes_ask": 0.01},
        {"market_metadata": {**MARKET_DATA, "floor_strike": None}},
        {"market_metadata": {**MARKET_DATA, "status": "closed"}},
        {"market_metadata": {**MARKET_DATA, "floor_strike": float("nan")}},
    ],
)
def test_bad_data_never_produces_order_proposal(change: dict) -> None:
    decision = opportunity(
        {**payload(), **change}, SERIES_DATA, history(), None, now=NOW, available_cents=5000
    )
    assert decision.side is None and decision.count == 0 and decision.blockers


def test_planner_no_calibration_no_cash_insufficient_depth() -> None:
    d = opportunity(payload(), SERIES_DATA, history(), None, now=NOW, available_cents=5000)
    assert d.side is None and any("calibration" in b for b in d.blockers)
    with (
        patch("kalshi_bot.btc_automation.estimate", return_value=Estimate(0.9, 0.001, 121, 300)),
        patch(
            "kalshi_bot.btc_automation.calibration_check",
            return_value={"passed": True, "probability_haircut": 0.05},
        ),
    ):
        assert (
            opportunity(payload(), SERIES_DATA, history(), None, now=NOW, available_cents=1).side
            is None
        )
        p = payload()
        p["raw_book"]["no_dollars"] = [["0.5500", "3.99"]]
        p["yes_ask_depth"] = 3.99
        assert (
            opportunity(p, SERIES_DATA, history(), None, now=NOW, available_cents=5000).side is None
        )


@pytest.mark.parametrize("p,side", [(0.62, "yes"), (0.23, "no")])
def test_exact_eight_point_boundary_is_inclusive_for_both_sides(p: float, side: str) -> None:
    with (
        patch("kalshi_bot.btc_automation.estimate", return_value=Estimate(p, 0.001, 121, 300)),
        patch(
            "kalshi_bot.btc_automation.calibration_check",
            return_value={"passed": True, "probability_haircut": 0.05},
        ),
    ):
        d = opportunity(payload(), SERIES_DATA, history(), None, now=NOW, available_cents=5000)
        assert d.side == side
        assert getattr(d, side + "_edge") == 0.08


def test_journal_sticky_kill_restart_and_never_dispatch(tmp_path: Path) -> None:
    path, paper = tmp_path / "auto.db", tmp_path / "paper.db"
    journal, broker = ExecutionJournal(path), PaperBroker(paper)
    before = ledger_state(paper)
    try:
        with pytest.raises(ValueError, match="kill"):
            journal.claim(staged(), now=NOW, cash_cents=5000)
        disarm_fixture_only(journal)
        with pytest.raises(SimulationOnlyError):
            journal.dispatch(staged(), broker, now=NOW)
        assert journal.db.execute("SELECT COUNT(*) FROM auto_intents").fetchone()[0] == 0
        assert ledger_state(paper) == before
        journal.kill("operator stop", NOW)
        journal.close()
        journal = ExecutionJournal(path)
        with pytest.raises(ValueError, match="kill"):
            journal.claim(staged(), now=NOW, cash_cents=5000)
    finally:
        journal.close()
        broker.close()


def test_two_workers_idempotency_and_global_overlap(tmp_path: Path) -> None:
    path = tmp_path / "auto.db"
    first, second = ExecutionJournal(path), ExecutionJournal(path)
    try:
        disarm_fixture_only(first)
        first.claim(staged(), now=NOW, cash_cents=5000)
        with pytest.raises(ValueError, match="Duplicate"):
            second.claim(staged(), now=NOW, cash_cents=5000)
        with pytest.raises(ValueError, match="overlapping"):
            second.claim(staged("KXBTC15M-OTHER-15"), now=NOW, cash_cents=5000)
        first.close()
        first = ExecutionJournal(path)
        with pytest.raises(ValueError, match="overlapping"):
            first.claim(staged("KXBTC15M-OTHER-15"), now=NOW, cash_cents=5000)
        assert first.db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        first.close()
        second.close()


def test_daily_gross_loss_gains_do_not_replenish_budget(tmp_path: Path) -> None:
    j = ExecutionJournal(tmp_path / "auto.db")
    try:
        disarm_fixture_only(j)
        for i, pnl in enumerate([-100, -52, 1000]):
            d = staged(f"KXBTC15M-{i}-15", NOW - 3 + i)
            j.db.execute(
                "INSERT INTO auto_intents VALUES(?,?,?,?,?,'reconciled',?)",
                (d.ticker, d.client_id, d.observed_at, "yes", 100, "{}"),
            )
            j.db.execute(
                "INSERT INTO auto_results VALUES(?,?,?,?)", (d.ticker, NOW, pnl, "isolated fixture")
            )
        with pytest.raises(ValueError, match="Daily"):
            j.claim(staged(), now=NOW, cash_cents=5000)
        # No automatic carry-forward of yesterday's budget; active exposure still blocks.
        tomorrow = int(NOW // 86400) * 86400 + 86401
        j.claim(replace(staged(), observed_at=tomorrow), now=tomorrow, cash_cents=5000)
    finally:
        j.close()


@pytest.mark.parametrize(
    "change",
    [
        {"maximum_loss_cents": 101},
        {"count": 2},
        {"maximum_loss_cents": 1},
        {"observed_at": NOW - 2},
        {"blockers": ("not validated",)},
    ],
)
def test_invalid_staging_reservation_rejected(tmp_path: Path, change: dict) -> None:
    j = ExecutionJournal(tmp_path / "auto.db")
    try:
        disarm_fixture_only(j)
        with pytest.raises(ValueError):
            j.claim(replace(staged(), **change), now=NOW, cash_cents=5000)
    finally:
        j.close()


def test_dashboard_readonly_no_fabrication_and_html_escaping(tmp_path: Path) -> None:
    path = tmp_path / "paper.db"
    broker = PaperBroker(path)
    broker.close()
    before = ledger_state(path)
    data = snapshot(path, now=NOW)
    assert data["bankroll_cents"] == 5000 and data["trades"] == 0
    assert data["win_rate"] is None and data["maximum_drawdown"] == 0
    assert not data["entries_enabled"] and not data["live_enabled"]
    data["errors"] = ["<script>alert(1)</script>"]
    page = render(data)
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "P(YES)" in page and "P(NO)" in page and "No fills" in page
    assert ledger_state(path) == before
    missing = snapshot(tmp_path / "missing.db", now=NOW)
    assert missing["bankroll_cents"] is None and missing["errors"]
    assert not (tmp_path / "missing.db").exists()


def test_dashboard_has_no_fabricated_drawdown_after_unrecorded_trade(tmp_path: Path) -> None:
    path = tmp_path / "paper.db"
    PaperBroker(path).close()
    with sqlite3.connect(path) as db:
        # Raw fixture only, never an execution authorization.
        db.execute("INSERT INTO fills VALUES(1,'fixture','T','yes','buy',1,40,2,?)", (NOW,))
    assert snapshot(path, now=NOW)["maximum_drawdown"] is None


def test_diagnostic_callback_logs_rejected_signal_without_reserving(tmp_path: Path) -> None:
    j = ExecutionJournal(tmp_path / "auto.db")
    try:
        d = j.analyze(payload(), SERIES_DATA, history(), None, now=NOW, available_cents=5000)
        assert d.blockers
        assert j.db.execute("SELECT COUNT(*) FROM auto_events").fetchone()[0] == 1
        assert j.db.execute("SELECT COUNT(*) FROM auto_intents").fetchone()[0] == 0
    finally:
        j.close()


def test_dashboard_archived_quotes_are_never_current_edges(tmp_path: Path) -> None:
    import json

    from kalshi_bot.btc_observe import Observer

    paper, archive = tmp_path / "paper.db", tmp_path / "obs"
    PaperBroker(paper).close()
    observer = Observer(archive)
    observer.record_observation(NOW, MARKET_DATA["ticker"], True, False, False, payload())
    observer.db.commit()
    archive_path = Path(observer.db.execute("PRAGMA database_list").fetchone()[2])
    observer.db.close()
    data = snapshot(paper, archive_path, now=NOW + 100)
    assert not data["errors"]
    assert data["signals"][0]["historical"]
    assert data["signals"][0]["strict_yes_net_edge"] is None
    assert not data["signals"][0]["strict_execution_eligible"]
    assert "NaN" not in json.dumps(data, allow_nan=False)
