"""Backtest every bundled strategy on synthetic data and print a report.

Runs fully offline — no Kalshi credentials or network access required. This is
the programmatic equivalent of ``kalshi-bot backtest <strategy>``.

Usage:
    python examples/backtest_demo.py

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from rich.console import Console

from kalshi_bot.backtesting.data import random_walk_snapshots
from kalshi_bot.backtesting.engine import Backtester
from kalshi_bot.backtesting.report import render
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.strategies.base import Strategy
from kalshi_bot.strategies.examples.arbitrage import ArbitrageYesNo
from kalshi_bot.strategies.examples.fair_value import FairValue
from kalshi_bot.strategies.examples.market_maker import MarketMaker
from kalshi_bot.strategies.examples.mean_reversion import MeanReversion
from kalshi_bot.strategies.examples.momentum import Momentum


def build_strategies() -> dict[str, Strategy]:
    """Instantiate one of each bundled example strategy."""
    return {
        "momentum": Momentum(window=10, size=1),
        "mean_reversion": MeanReversion(window=20, z=1.5, size=1),
        "market_maker": MarketMaker(spread_cents=2, quote_size=1, max_inventory=20),
        "arbitrage": ArbitrageYesNo(size=1, min_edge_cents=1),
        "fair_value": FairValue(fair_prob=0.6, margin_cents=3, max_position=30),
    }


def main() -> None:
    """Run each strategy through the backtester and render its metrics."""
    console = Console()
    snapshots = random_walk_snapshots(ticker="DEMO-MARKET", n=250, seed=42)
    # A 1c-per-contract fee makes the simulation a touch more realistic.
    backtester = Backtester(
        starting_balance_cents=100_000,
        risk=RiskManager(),
        fee_cents_per_contract=1,
    )

    console.print(
        "[bold]Kalshi Trading Bot[/] — offline backtest demo "
        "([dim]educational only, not financial advice[/])\n"
    )
    for name, strategy in build_strategies().items():
        report = backtester.run(strategy, snapshots)
        render(report, console=console, title=f"Strategy: {name}")
        console.print()


if __name__ == "__main__":
    main()
