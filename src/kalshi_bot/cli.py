"""Command-line interface for the Kalshi Trading Bot.

Run ``kalshi-bot --help`` after installing, or ``python -m kalshi_bot --help``.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from kalshi_bot import __version__
from kalshi_bot.backtesting.data import random_walk_snapshots
from kalshi_bot.backtesting.engine import Backtester
from kalshi_bot.backtesting.report import render as render_report
from kalshi_bot.config import load_settings
from kalshi_bot.core.engine import TradingEngine
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.paper import PaperBroker
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.strategies.base import Strategy
from kalshi_bot.strategies.examples.arbitrage import ArbitrageYesNo
from kalshi_bot.strategies.examples.fair_value import FairValue
from kalshi_bot.strategies.examples.market_maker import MarketMaker
from kalshi_bot.strategies.examples.mean_reversion import MeanReversion
from kalshi_bot.strategies.examples.momentum import Momentum

app = typer.Typer(
    add_completion=False,
    help="Kalshi Trading Bot — open-source framework by Viprasol Tech.",
)
console = Console()

STRATEGIES: dict[str, type[Strategy]] = {
    "market_maker": MarketMaker,
    "momentum": Momentum,
    "mean_reversion": MeanReversion,
    "arbitrage": ArbitrageYesNo,
    "fair_value": FairValue,
}


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"kalshi-trading-bot [bold cyan]{__version__}[/] — by Viprasol Tech")


@app.command()
def strategies() -> None:
    """List the bundled example strategies."""
    table = Table(title="Bundled strategies (educational only)")
    table.add_column("Name", style="cyan")
    table.add_column("Class")
    table.add_column("Summary")
    summaries = {
        "market_maker": "Quote both sides around mid with an inventory cap.",
        "momentum": "Trade in the direction of short-term last-price momentum.",
        "mean_reversion": "Fade moves beyond N standard deviations from the mean.",
        "arbitrage": "Buy YES+NO when their combined ask is below 100c.",
        "fair_value": "Buy YES below your fair probability, Kelly-sized.",
    }
    for name, cls in STRATEGIES.items():
        table.add_row(name, cls.__name__, summaries.get(name, ""))
    console.print(table)


@app.command()
def backtest(
    strategy: str = typer.Argument("momentum", help=f"One of: {', '.join(STRATEGIES)}"),
    ticks: int = typer.Option(200, help="Number of synthetic snapshots to replay."),
    balance: int = typer.Option(5000, help="Paper balance in cents (maximum 5000)."),
    seed: int = typer.Option(42, help="RNG seed for the synthetic random walk."),
) -> None:
    """Backtest a strategy on synthetic data and print a metrics report.

    Uses an offline random walk so it runs with no network or credentials.
    """
    if strategy not in STRATEGIES:
        choices = ", ".join(STRATEGIES)
        console.print(f"[red]Unknown strategy '{strategy}'. Choose from: {choices}[/]")
        raise typer.Exit(code=1)

    snapshots = random_walk_snapshots(n=ticks, seed=seed)
    bt = Backtester(starting_balance_cents=balance)
    report = bt.run(STRATEGIES[strategy](), snapshots)
    render_report(report, console=console, title=f"Backtest: {strategy} ({ticks} ticks)")


@app.command()
def markets(
    status: str = typer.Option("open", help="Filter by market status."),
    limit: int = typer.Option(10, help="Maximum markets to list."),
) -> None:
    """List markets from Kalshi (public data, no credentials needed)."""

    async def _run() -> None:
        settings = load_settings()
        async with KalshiClient.from_settings(settings) as client:
            rows = await client.get_markets(status=status, limit=limit)
            table = Table(title=f"Kalshi markets ({settings.environment.value})")
            table.add_column("Ticker", style="cyan")
            table.add_column("Title")
            table.add_column("Yes Bid", justify="right")
            table.add_column("Yes Ask", justify="right")
            for m in rows:
                table.add_row(m.ticker, m.title or "-", str(m.yes_bid), str(m.yes_ask))
            console.print(table)

    asyncio.run(_run())


@app.command("paper-status")
def paper_status() -> None:
    """Show the persistent $50 paper ledger; never query an exchange account."""
    broker = PaperBroker()
    try:
        for name, value in broker.account().items():
            console.print(f"{name}: {value} cents")
        console.print(f"pending_orders: {len(broker.pending())}")
        console.print(f"fills: {len(broker.rows('fills'))}")
        console.print(f"settlements: {len(broker.rows('settlements'))}")
    finally:
        broker.close()


@app.command("btc-paper")
def btc_paper(
    ticks: int = typer.Option(1, min=1, max=1800, help="Bounded paper polling cycles."),
    interval: float = typer.Option(2.0, min=1, max=5, help="Seconds between observations."),
    calibration: Path | None = typer.Option(None, help="Observed out-of-time calibration JSON."),
    record: Path | None = typer.Option(
        None,
        help="Append normalized observations to a .jsonl file.",
    ),
) -> None:
    """Discover verified demo BTC 15-minute markets; never submit real orders."""
    from kalshi_bot.btc_data import CoinbaseReference, DemoBTCSource
    from kalshi_bot.btc_model import CalibrationArtifact
    from kalshi_bot.btc_replay import append_observation
    from kalshi_bot.btc_strategy import BTCPaperRunner

    artifact = None
    if record is not None and record.suffix != ".jsonl":
        raise typer.BadParameter("Observation recording requires a .jsonl data file.")
    if calibration is not None:
        if calibration.stat().st_size > 5_000_000:
            raise typer.BadParameter("Calibration artifact exceeds 5 MB.")
        artifact = CalibrationArtifact.model_validate_json(calibration.read_text())

    async def _run() -> None:
        settings = load_settings()
        broker = PaperBroker(risk=RiskManager.from_settings(settings.risk))
        reference = CoinbaseReference()
        try:
            async with KalshiClient.from_settings(settings) as client:
                runner = BTCPaperRunner(
                    broker,
                    DemoBTCSource(client),
                    reference,
                    artifact,
                    recorder=(lambda event: append_observation(record, event))
                    if record is not None
                    else None,
                )
                report = await runner.run(ticks=ticks, interval=interval)
                console.print_json(data=report)
        finally:
            await reference.close()
            broker.close()

    asyncio.run(_run())


@app.command("btc-history-audit")
def btc_history_audit(
    limit: int = typer.Option(200, min=1, max=200),
    output: Path = typer.Option(Path("data/btc-history-audit.json")),
) -> None:
    """Audit public demo labels/candles; never manufacture executable depth."""
    from kalshi_bot.btc_data import DemoBTCSource
    from kalshi_bot.btc_history import history_audit

    async def _run() -> None:
        settings = load_settings()
        async with KalshiClient.from_settings(settings) as client:
            report = await history_audit(DemoBTCSource(client), limit)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        summary = {
            k: v
            for k, v in report.items()
            if k not in {"candlestick_probes", "raw_finalized_markets", "coinbase_candles"}
        }
        summary["output"] = str(output)
        summary["candle_probe_count"] = len(report["candlestick_probes"])
        console.print_json(data=summary)

    asyncio.run(_run())


@app.command("btc-replay")
def btc_replay(
    observations: Path = typer.Argument(..., exists=True, dir_okay=False),
    output: Path = typer.Option(Path("data/btc-replay-report.json")),
) -> None:
    """Replay recorded public observations offline; no calibration bypass."""
    from kalshi_bot.btc_replay import replay

    async def _run() -> None:
        # No artifact creation/loading or promotion: observation-only evaluation.
        blocked = False
        try:
            report, events = await replay(observations)
        except (ValueError, OSError) as exc:
            blocked = True
            report = {
                "status": "blocked_invalid_or_inadequate_archive",
                "reason": str(exc),
                "historical_win_rate": None,
                "historical_pnl_cents": None,
                "historical_maximum_drawdown": None,
                "historical_calibration": None,
                "promotion_allowed": False,
                "persistent_bankroll_modified": False,
            }
            events = []
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        audit_path = output.with_name(output.stem + "-events.jsonl")
        audit_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )
        console.print_json(data=report)
        if blocked:
            raise typer.Exit(code=2)

    asyncio.run(_run())


@app.command()
def balance() -> None:
    """Exchange balances are disabled; use paper-status for the simulated account."""
    console.print("[red]Exchange account access is disabled. Use paper-status; do not add keys.[/]")
    raise typer.Exit(code=1)


@app.command()
def run(
    strategy: str = typer.Argument(..., help=f"One of: {', '.join(STRATEGIES)}"),
    ticker: str = typer.Argument(..., help="Market ticker to trade."),
    ticks: int = typer.Option(5, help="Number of poll cycles before stopping."),
    live: bool = typer.Option(False, "--live", help="Blocked on this simulation-only branch."),
) -> None:
    """Run a paper strategy using unsigned public demo market data."""
    if live:
        console.print("[red]Simulation-only: --live is disabled.[/]")
        raise typer.Exit(code=1)
    if strategy not in STRATEGIES:
        choices = ", ".join(STRATEGIES)
        console.print(f"[red]Unknown strategy '{strategy}'. Choose from: {choices}[/]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        settings = load_settings()
        # Settings and both execution boundaries enforce simulation-only operation.
        dry_run = settings.dry_run
        async with KalshiClient.from_settings(settings) as client:
            engine = TradingEngine(
                client=client,
                strategy=STRATEGIES[strategy](),
                risk=RiskManager.from_settings(settings.risk),
                dry_run=dry_run,
                poll_interval=settings.poll_interval,
            )
            await engine.run(ticker, max_ticks=ticks)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
