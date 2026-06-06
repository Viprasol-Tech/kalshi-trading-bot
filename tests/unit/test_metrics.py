"""Tests for backtest performance metrics."""

from __future__ import annotations

import math

from kalshi_bot.backtesting import metrics


def test_returns_from_equity_basic() -> None:
    rets = metrics.returns_from_equity([100, 110, 99])
    assert rets[0] == 0.10
    assert math.isclose(rets[1], (99 - 110) / 110)


def test_returns_skips_zero_prev() -> None:
    assert metrics.returns_from_equity([0, 100, 110]) == [0.10]


def test_total_return() -> None:
    assert metrics.total_return([100, 150]) == 0.5
    assert metrics.total_return([100]) == 0.0
    assert metrics.total_return([]) == 0.0


def test_max_drawdown_monotonic_is_zero() -> None:
    assert metrics.max_drawdown([100, 101, 102, 103]) == 0.0


def test_max_drawdown_detects_decline() -> None:
    # Peak 200, trough 150 -> 25% drawdown.
    dd = metrics.max_drawdown([100, 200, 150, 180])
    assert math.isclose(dd, 0.25)


def test_volatility_few_samples_is_zero() -> None:
    assert metrics.volatility([0.01]) == 0.0


def test_volatility_positive() -> None:
    assert metrics.volatility([0.01, -0.02, 0.03, -0.01]) > 0.0


def test_sharpe_zero_when_flat() -> None:
    assert metrics.sharpe_ratio([0.0, 0.0, 0.0]) == 0.0


def test_sharpe_positive_for_upward_returns() -> None:
    assert metrics.sharpe_ratio([0.01, 0.012, 0.011, 0.009]) > 0.0


def test_win_rate() -> None:
    assert metrics.win_rate([1.0, -1.0, 2.0, -0.5]) == 0.5
    assert metrics.win_rate([]) == 0.0


def test_profit_factor_no_losses_is_inf() -> None:
    assert math.isinf(metrics.profit_factor([1.0, 2.0]))


def test_profit_factor_ratio() -> None:
    assert metrics.profit_factor([3.0, -1.0, -2.0]) == 1.0
