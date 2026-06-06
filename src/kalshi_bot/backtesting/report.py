"""Render a :class:`BacktestReport` as a rich console table.

Kept separate from the engine so the engine has no presentation dependency and
stays trivially unit-testable.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import math

from rich.console import Console
from rich.table import Table

from kalshi_bot.backtesting.engine import BacktestReport


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.2f}%"


def _fmt_ratio(value: float) -> str:
    return "inf" if math.isinf(value) else f"{value:.3f}"


def build_table(report: BacktestReport, title: str = "Backtest report") -> Table:
    """Build a two-column rich table of headline metrics for ``report``."""
    table = Table(title=title)
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", justify="right")

    table.add_row("Trades", str(report.trades))
    table.add_row("Net PnL", f"${report.pnl_dollars:,.2f}")
    table.add_row("Total return", _fmt_pct(report.total_return))
    table.add_row("Sharpe (annualised)", _fmt_ratio(report.sharpe))
    table.add_row("Max drawdown", _fmt_pct(report.max_drawdown))
    table.add_row("Volatility (annualised)", _fmt_pct(report.volatility))
    table.add_row("Win rate", _fmt_pct(report.win_rate))
    table.add_row("Profit factor", _fmt_ratio(report.profit_factor))
    table.add_row("Fees paid", f"${report.fees_paid_cents / 100:,.2f}")
    table.add_row("Final position", str(report.final_position))
    return table


def render(
    report: BacktestReport, console: Console | None = None, title: str = "Backtest report"
) -> None:
    """Print ``report`` to ``console`` (a fresh one if not supplied)."""
    (console or Console()).print(build_table(report, title=title))
