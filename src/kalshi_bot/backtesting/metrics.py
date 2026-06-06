"""Performance metrics for backtests.

Pure functions that turn an equity curve (and a trade log) into the standard
quant statistics: total/percent return, annualised Sharpe ratio, maximum
drawdown, volatility and win rate. All inputs are plain numbers so the functions
are trivially testable and have no exchange dependencies.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise


def returns_from_equity(equity_curve: Sequence[float]) -> list[float]:
    """Simple period-over-period returns from an equity curve.

    A flat or empty curve yields an empty list. Periods where the prior equity
    is zero are skipped to avoid division by zero.
    """
    out: list[float] = []
    for prev, cur in pairwise(equity_curve):
        if prev == 0:
            continue
        out.append((cur - prev) / prev)
    return out


def total_return(equity_curve: Sequence[float]) -> float:
    """Fractional return from the first to the last equity point."""
    if len(equity_curve) < 2 or equity_curve[0] == 0:
        return 0.0
    return (equity_curve[-1] - equity_curve[0]) / equity_curve[0]


def volatility(returns: Sequence[float], periods_per_year: int = 252) -> float:
    """Annualised standard deviation of returns (0.0 for < 2 samples)."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    var = sum((r - mean) ** 2 for r in returns) / (n - 1)
    return math.sqrt(var) * math.sqrt(periods_per_year)


def sharpe_ratio(
    returns: Sequence[float],
    risk_free_rate: float = 0.0,
    periods_per_year: int = 252,
) -> float:
    """Annualised Sharpe ratio of a return series.

    ``risk_free_rate`` is expressed per period. Returns 0.0 when there are too
    few samples or the return volatility is zero.
    """
    n = len(returns)
    if n < 2:
        return 0.0
    excess = [r - risk_free_rate for r in returns]
    mean = sum(excess) / n
    var = sum((r - mean) ** 2 for r in excess) / (n - 1)
    std = math.sqrt(var)
    if std == 0:
        return 0.0
    return (mean / std) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: Sequence[float]) -> float:
    """Largest peak-to-trough fractional decline (a non-negative number).

    A monotonically rising curve has a drawdown of 0.0. The result is the
    magnitude of the worst drop, e.g. 0.25 for a 25% decline from a peak.
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    worst = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            dd = (peak - value) / peak
            worst = max(worst, dd)
    return worst


def win_rate(trade_pnls: Sequence[float]) -> float:
    """Fraction of trades with strictly positive PnL (0.0 if no trades)."""
    if not trade_pnls:
        return 0.0
    wins = sum(1 for p in trade_pnls if p > 0)
    return wins / len(trade_pnls)


def profit_factor(trade_pnls: Sequence[float]) -> float:
    """Gross profit divided by gross loss.

    Returns ``inf`` when there are profits but no losses, and 0.0 when there are
    no profitable trades.
    """
    gross_profit = sum(p for p in trade_pnls if p > 0)
    gross_loss = -sum(p for p in trade_pnls if p < 0)
    if gross_loss == 0:
        return math.inf if gross_profit > 0 else 0.0
    return gross_profit / gross_loss
