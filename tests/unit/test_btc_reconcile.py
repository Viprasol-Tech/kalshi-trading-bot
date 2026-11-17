"""Isolated paper fixtures ONLY. Runtime gates are never relaxed by this module."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from kalshi_bot.btc_automation import ExecutionJournal
from kalshi_bot.btc_observe import ledger_state
from kalshi_bot.btc_reconcile import reconcile_paper
from kalshi_bot.btc_rules import verify_market
from kalshi_bot.exchange.models import Action, OrderRequest, Side
from kalshi_bot.paper import PaperBroker
from tests.unit.test_btc import MARKET_DATA, NOW, OPEN, SERIES_DATA, paper_quote
from tests.unit.test_btc_automation import disarm_fixture_only, staged

TICKER = MARKET_DATA["ticker"]


@pytest.fixture
def fixture_pair(tmp_path: Path, offline_execution_fixture: None):
    """Test-only broker permission; no production/persistent user data."""
    journal = ExecutionJournal(tmp_path / "journal.db")
    broker = PaperBroker(tmp_path / "paper.db")
    disarm_fixture_only(journal)
    decision = replace(
        staged(), rules_sha256=verify_market(MARKET_DATA, SERIES_DATA, now=NOW).rules_sha256
    )
    journal.claim(decision, now=NOW, cash_cents=5000)
    oid = broker.submit(
        OrderRequest(
            ticker=TICKER,
            side=Side.YES,
            action=Action.BUY,
            count=1,
            yes_price=47,
            client_order_id=decision.client_id,
        ),
        now=NOW,
    )
    assert oid is not None
    yield journal, broker, tmp_path / "paper.db", oid
    broker.close()
    journal.close()


def fill_entry(broker: PaperBroker) -> None:
    assert (
        broker.process_quote(
            paper_quote(NOW + 1, yes_bid=40, yes_ask=44, no_bid=56, no_ask=60), now=NOW + 1
        )
        == 1
    )


def test_pending_then_cancel_release_and_exact_restart_idempotency(fixture_pair) -> None:
    journal, broker, path, oid = fixture_pair
    before = ledger_state(path)
    result = reconcile_paper(journal, path, TICKER, now=NOW + 1)
    assert not result["reconciled"] and result["pending_orders"] == 1
    assert ledger_state(path) == before
    broker.cancel(oid, now=NOW + 2)
    result = reconcile_paper(journal, path, TICKER, now=NOW + 2)
    assert result["reconciled"] and result["pnl_cents"] == 0
    journal_path = Path(journal.db.execute("PRAGMA database_list").fetchone()[2])
    other = ExecutionJournal(journal_path)
    try:
        reconcile_paper(other, path, TICKER, now=NOW + 3)
        assert other.db.execute("SELECT COUNT(*) FROM auto_results").fetchone()[0] == 1
        with pytest.raises(ValueError, match="Duplicate"):
            other.claim(replace(staged(), observed_at=NOW + 3), now=NOW + 3, cash_cents=5000)
    finally:
        other.close()


def test_filled_entry_keeps_risk_until_sell_fully_reconciled(fixture_pair) -> None:
    journal, broker, path, _ = fixture_pair
    fill_entry(broker)
    result = reconcile_paper(journal, path, TICKER, now=NOW + 2)
    assert result["inventory"] == 1 and not result["reconciled"]
    broker.submit(
        OrderRequest(
            ticker=TICKER,
            side=Side.YES,
            action=Action.SELL,
            count=1,
            yes_price=60,
            client_order_id="test-exit",
        ),
        now=NOW + 2,
    )
    assert not reconcile_paper(journal, path, TICKER, now=NOW + 2)["reconciled"]
    broker.process_quote(
        paper_quote(NOW + 3, yes_bid=65, yes_ask=66, no_bid=34, no_ask=35), now=NOW + 3
    )
    result = reconcile_paper(journal, path, TICKER, now=NOW + 4)
    assert result["reconciled"] and result["pnl_cents"] == broker.account()["realized_pnl"]
    assert result["fees_cents"] == 4


def finalized() -> dict:
    return {
        **MARKET_DATA,
        "status": "finalized",
        "result": "yes",
        "settlement_ts": "2026-11-11T15:16:00Z",
        "settlement_value_dollars": "1.0000",
    }


def test_verified_authoritative_settlement_and_retry(fixture_pair) -> None:
    journal, broker, path, _ = fixture_pair
    fill_entry(broker)
    broker.settle(
        TICKER,
        yes_payout_cents=100,
        confirmed=True,
        source="https://demo-api.kalshi.co/trade-api/v2/markets/" + TICKER,
        now=OPEN + 1000,
    )
    before = ledger_state(path)
    result = reconcile_paper(
        journal, path, TICKER, now=OPEN + 1000, final_market=finalized(), series=SERIES_DATA
    )
    assert result["reconciled"] and result["pnl_cents"] == 53
    reconcile_paper(
        journal, path, TICKER, now=OPEN + 1001, final_market=finalized(), series=SERIES_DATA
    )
    assert ledger_state(path) == before
    assert journal.db.execute("SELECT COUNT(*) FROM auto_results").fetchone()[0] == 1
    with pytest.raises(ValueError, match="failed closed"):
        reconcile_paper(
            journal,
            path,
            TICKER,
            now=OPEN + 1002,
            final_market={**finalized(), "floor_strike": None},
            series=SERIES_DATA,
        )
    assert journal.db.execute("SELECT killed FROM auto_control").fetchone()[0] == 1


def test_conflicting_receipt_kills_without_risk_release(fixture_pair) -> None:
    journal, broker, path, _ = fixture_pair
    fill_entry(broker)
    reconcile_paper(journal, path, TICKER, now=NOW + 2)
    broker.db.execute("UPDATE fills SET price=price-1")
    with pytest.raises(ValueError, match="failed closed"):
        reconcile_paper(journal, path, TICKER, now=NOW + 3)
    assert journal.db.execute("SELECT killed FROM auto_control").fetchone()[0] == 1
    assert journal.db.execute("SELECT COUNT(*) FROM auto_results").fetchone()[0] == 0


def test_missing_ack_never_becomes_permission_to_resubmit(fixture_pair) -> None:
    journal, broker, path, oid = fixture_pair
    broker.db.execute("DELETE FROM orders WHERE id=?", (oid,))
    with pytest.raises(ValueError, match="failed closed"):
        reconcile_paper(journal, path, TICKER, now=NOW + 1)
    assert journal.db.execute("SELECT status FROM auto_intents").fetchone()[0] == "reserved"


def test_partial_history_and_unapproved_size_fail_closed(fixture_pair) -> None:
    journal, broker, path, oid = fixture_pair
    fill_entry(broker)
    # Existing simulator uses whole contracts. Fabricated 1-of-2 partial history
    # must NOT silently enlarge the reserved one-contract BTC proposal.
    row = broker.db.execute("SELECT payload FROM orders WHERE id=?", (oid,)).fetchone()
    request = json.loads(row[0])
    request["count"] = 2
    broker.db.execute(
        "UPDATE orders SET payload=?,remaining=1,reserved=49,status='pending' WHERE id=?",
        (json.dumps(request), oid),
    )
    with pytest.raises(ValueError, match="failed closed"):
        reconcile_paper(journal, path, TICKER, now=NOW + 2)
    assert journal.db.execute("SELECT COUNT(*) FROM auto_results").fetchone()[0] == 0


def test_crash_at_terminal_journal_write_rolls_back_then_recovers(fixture_pair) -> None:
    journal, broker, path, oid = fixture_pair
    broker.cancel(oid, now=NOW + 1)
    before = ledger_state(path)
    with (
        patch.object(journal, "event", side_effect=RuntimeError("synthetic process loss")),
        pytest.raises(RuntimeError, match="process loss"),
    ):
        reconcile_paper(journal, path, TICKER, now=NOW + 2)
    assert journal.db.execute("SELECT COUNT(*) FROM auto_results").fetchone()[0] == 0
    assert journal.db.execute("SELECT status FROM auto_intents").fetchone()[0] == "reserved"
    other = ExecutionJournal(Path(journal.db.execute("PRAGMA database_list").fetchone()[2]))
    try:
        result = reconcile_paper(other, path, TICKER, now=NOW + 3)
        assert result["reconciled"]
        assert other.db.execute("SELECT COUNT(*) FROM auto_results").fetchone()[0] == 1
        assert ledger_state(path) == before
    finally:
        other.close()
