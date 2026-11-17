"""Isolated synthetic lifecycle drill, NOT an approved paper trial.

Only these temporary fixtures patch the sealed entry gate. Runtime flags,
the real user account and network permissions are never changed.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from kalshi_bot.btc_automation import ExecutionJournal, exit_plan
from kalshi_bot.btc_dashboard import snapshot
from kalshi_bot.btc_observe import book_fields, ledger_state
from kalshi_bot.btc_reconcile import reconcile_paper
from kalshi_bot.btc_rules import verify_market
from kalshi_bot.exchange.models import Side
from kalshi_bot.paper import PaperBroker
from kalshi_bot.safety import SimulationOnlyError
from tests.unit.test_btc import MARKET_DATA, NOW, SERIES_DATA, paper_quote
from tests.unit.test_btc_automation import disarm_fixture_only, staged
from tests.unit.test_btc_readiness import executable_payload

TICKER = MARKET_DATA["ticker"]
EXIT_AT = NOW + 250


def payload(side: str = "yes") -> dict:
    p = {**executable_payload(), "market_metadata": MARKET_DATA}
    p.update(
        book_fields(
            {
                "orderbook_fp": {
                    "yes_dollars": [[".60" if side == "yes" else ".35", "8"]],
                    "no_dollars": [[".35" if side == "yes" else ".60", "8"]],
                }
            }
        )
    )
    p.update(quote_timestamp=EXIT_AT, quote_timestamp_raw=EXIT_AT)
    for name in ("book", "reference", "metadata"):
        p[name + "_received_at"] = EXIT_AT
    return p


@pytest.fixture
def pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offline_execution_fixture: None):
    monkeypatch.setattr("kalshi_bot.btc_automation.require_entry_release", lambda: None)
    journal = ExecutionJournal(tmp_path / "journal.db")
    broker = PaperBroker(tmp_path / "trial.db", slippage_cents=2)
    disarm_fixture_only(journal)
    yield journal, broker, tmp_path / "trial.db"
    broker.close()
    journal.close()


def enter(journal: ExecutionJournal, broker: PaperBroker, side: str = "yes") -> None:
    decision = replace(
        staged(),
        side=side,
        rules_sha256=verify_market(MARKET_DATA, SERIES_DATA, now=NOW).rules_sha256,
    )
    assert journal.dispatch(decision, broker, now=NOW)
    changes = (
        {"yes_bid": 40, "yes_ask": 44, "no_bid": 56, "no_ask": 60}
        if side == "yes"
        else {
            "yes_bid": 56,
            "yes_ask": 60,
            "no_bid": 40,
            "no_ask": 44,
        }
    )
    assert broker.process_quote(paper_quote(NOW + 1, **changes), now=NOW + 1) == 1


@pytest.mark.parametrize("side", ["yes", "no"])
def test_automatic_adapters_enter_monitor_exit_reconcile_restart(pair, side: str) -> None:
    journal, broker, path = pair
    enter(journal, broker, side)
    assert not reconcile_paper(journal, path, TICKER, now=EXIT_AT)["reconciled"]
    plan = exit_plan(payload(side), SERIES_DATA, side, now=EXIT_AT)
    assert plan.limit_cents == 58 and not plan.blockers
    assert journal.dispatch_exit(plan, broker, now=EXIT_AT)
    with pytest.raises(ValueError):
        journal.dispatch_exit(plan, broker, now=EXIT_AT)  # duplicate pending sell
    quote = {"yes_bid": 60, "yes_ask": 65, "no_bid": 35, "no_ask": 40}
    if side == "no":
        quote = {"yes_bid": 35, "yes_ask": 40, "no_bid": 60, "no_ask": 65}
    assert broker.process_quote(paper_quote(EXIT_AT + 1, **quote), now=EXIT_AT + 1) == 1
    result = reconcile_paper(journal, path, TICKER, now=EXIT_AT + 2)
    assert result["reconciled"] and result["inventory"] == 0
    assert result["fees_cents"] == 4 and broker.slippage_cents == 2
    assert journal.db.execute("SELECT status FROM auto_exits").fetchone()[0] == "reconciled"
    journal_path = Path(journal.db.execute("PRAGMA database_list").fetchone()[2])
    display = snapshot(path, journal=journal_path, now=EXIT_AT + 2)
    assert display["win_rate"] == 1 and display["reconciled_closed_positions"] == 1
    assert display["maximum_drawdown"] is None  # no invented equity history
    other = PaperBroker(path, slippage_cents=2)
    try:
        assert other.account() == broker.account()
        assert other.holdings(TICKER, Side(side)) == 0
    finally:
        other.close()
    with pytest.raises(ValueError, match="policy mismatch"):
        PaperBroker(path)


def test_exit_requires_reconciliation_and_kill_retains_exposure(pair) -> None:
    journal, broker, path = pair
    enter(journal, broker)
    plan = exit_plan(payload(), SERIES_DATA, "yes", now=EXIT_AT)
    with pytest.raises(ValueError):
        journal.dispatch_exit(plan, broker, now=EXIT_AT)
    reconcile_paper(journal, path, TICKER, now=EXIT_AT)
    journal.kill("synthetic emergency shutdown", EXIT_AT)
    with pytest.raises(ValueError):
        journal.dispatch_exit(plan, broker, now=EXIT_AT)
    assert broker.holdings(TICKER, Side.YES) == 1 and broker.pending() == []
    assert journal.db.execute("SELECT COUNT(*) FROM auto_exits").fetchone()[0] == 0


def test_uncertain_exit_ack_blocks_retry_and_reconciliation_fails_closed(pair) -> None:
    journal, broker, path = pair
    enter(journal, broker)
    reconcile_paper(journal, path, TICKER, now=EXIT_AT)
    plan = exit_plan(payload(), SERIES_DATA, "yes", now=EXIT_AT)
    with (
        patch.object(broker, "submit", side_effect=RuntimeError("synthetic loss")),
        pytest.raises(RuntimeError),
    ):
        journal.dispatch_exit(plan, broker, now=EXIT_AT)
    assert journal.db.execute("SELECT killed FROM auto_control").fetchone()[0] == 1
    assert journal.db.execute("SELECT status FROM auto_exits").fetchone()[0] == "prepared"
    with pytest.raises(ValueError, match="failed closed"):
        reconcile_paper(journal, path, TICKER, now=EXIT_AT + 1)
    assert journal.db.execute("SELECT COUNT(*) FROM auto_results").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["stale", "no_bid", "thin", "negative_proceeds"])
def test_exit_never_invents_executable_bid_or_raises_prepaid_loss(change: str) -> None:
    p = payload()
    if change == "stale":
        p["quote_timestamp"] = EXIT_AT - 6
    else:
        raw = {
            "yes_dollars": []
            if change == "no_bid"
            else [
                [
                    ".03" if change == "negative_proceeds" else ".60",
                    "3.99" if change == "thin" else "8",
                ]
            ],
            "no_dollars": [[".35", "8"]],
        }
        p.update(book_fields({"orderbook_fp": raw}))
    plan = exit_plan(p, SERIES_DATA, "yes", now=EXIT_AT)
    assert plan.blockers and plan.limit_cents is None


def test_real_exit_release_gate_remains_sealed(tmp_path: Path) -> None:
    journal = ExecutionJournal(tmp_path / "journal.db")
    broker = PaperBroker(tmp_path / "paper.db")
    try:
        with pytest.raises(SimulationOnlyError):
            journal.dispatch_exit(
                exit_plan(payload(), SERIES_DATA, "yes", now=EXIT_AT), broker, now=EXIT_AT
            )
        assert broker.rows("orders") == [] and broker.rows("fills") == []
    finally:
        broker.close()
        journal.close()


def test_partial_fractional_fault_never_releases_reserved_risk(pair) -> None:
    journal, broker, path = pair
    enter(journal, broker)
    broker.db.execute("UPDATE fills SET quantity=0.5")  # explicitly corrupted synthetic receipt
    with pytest.raises(ValueError, match="failed closed"):
        reconcile_paper(journal, path, TICKER, now=NOW + 2)
    assert journal.db.execute("SELECT killed FROM auto_control").fetchone()[0] == 1
    assert journal.db.execute("SELECT COUNT(*) FROM auto_results").fetchone()[0] == 0


def test_existing_bankroll_cannot_be_repurposed_for_new_slippage_trial(tmp_path: Path) -> None:
    original = tmp_path / "paper.db"
    PaperBroker(original).close()
    before = ledger_state(original)
    with pytest.raises(ValueError, match="new separate ledger"):
        PaperBroker(original, slippage_cents=2)
    assert ledger_state(original) == before
