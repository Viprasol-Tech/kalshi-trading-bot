"""Tests for the backtester and example strategies."""

from __future__ import annotations

from kalshi_bot.backtesting.engine import Backtester
from kalshi_bot.exchange.models import Market
from kalshi_bot.strategies.examples.momentum import Momentum


def _snapshots() -> list[Market]:
    prices = [40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 55, 60, 58, 50, 45]
    return [Market(ticker="TEST", last_price=p, yes_bid=p - 1, yes_ask=p + 1) for p in prices]


def test_backtester_runs_momentum() -> None:
    bt = Backtester(starting_balance_cents=100_000)
    result = bt.run(Momentum(window=5, size=1), _snapshots())
    assert result.trades >= 1
    assert len(result.equity_curve) == len(_snapshots())
