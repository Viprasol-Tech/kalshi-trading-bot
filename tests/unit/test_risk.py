"""Tests for the risk manager."""

from __future__ import annotations

from kalshi_bot.exchange.models import Action, OrderRequest, OrderType, Side
from kalshi_bot.risk.manager import RiskLimits, RiskManager


def _order(count: int, price: int = 50) -> OrderRequest:
    return OrderRequest(
        ticker="TEST",
        action=Action.BUY,
        side=Side.YES,
        count=count,
        type=OrderType.LIMIT,
        yes_price=price,
    )


def test_rejects_oversized_order() -> None:
    rm = RiskManager(RiskLimits(max_contracts_per_order=10))
    decision = rm.check(_order(11), current_position=0)
    assert not decision.approved


def test_rejects_position_breach() -> None:
    rm = RiskManager(RiskLimits(max_position_per_market=100))
    decision = rm.check(_order(10), current_position=95)
    assert not decision.approved


def test_rejects_notional_breach() -> None:
    rm = RiskManager(RiskLimits(max_order_notional_cents=100))
    decision = rm.check(_order(10, price=50), current_position=0)  # 500c > 100c
    assert not decision.approved


def test_approves_valid_order() -> None:
    rm = RiskManager(RiskLimits())
    assert rm.check(_order(1), current_position=0).approved


def test_kelly_no_edge_returns_zero() -> None:
    rm = RiskManager()
    # edge below price -> no positive Kelly fraction.
    assert rm.kelly_size(edge_prob=0.40, price_cents=50, bankroll_cents=100_000) == 0


def test_kelly_with_edge_is_positive() -> None:
    rm = RiskManager(RiskLimits(kelly_fraction=0.25, max_contracts_per_order=10_000))
    size = rm.kelly_size(edge_prob=0.70, price_cents=50, bankroll_cents=100_000)
    assert size > 0
