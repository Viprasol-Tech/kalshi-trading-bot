"""Smoke tests for the Typer CLI (offline commands only)."""

from __future__ import annotations

from typer.testing import CliRunner

from kalshi_bot import __version__
from kalshi_bot.cli import app

runner = CliRunner()


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_strategies_command_lists_all() -> None:
    result = runner.invoke(app, ["strategies"])
    assert result.exit_code == 0
    for name in ("market_maker", "momentum", "mean_reversion", "arbitrage", "fair_value"):
        assert name in result.stdout


def test_backtest_command_runs() -> None:
    result = runner.invoke(app, ["backtest", "momentum", "--ticks", "50", "--seed", "7"])
    assert result.exit_code == 0
    assert "Sharpe" in result.stdout
    assert "Net PnL" in result.stdout


def test_backtest_unknown_strategy_errors() -> None:
    result = runner.invoke(app, ["backtest", "does_not_exist"])
    assert result.exit_code == 1
