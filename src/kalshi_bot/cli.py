"""Command-line interface for the Kalshi Trading Bot.

Run ``kalshi-bot --help`` after installing, or ``python -m kalshi_bot --help``.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from kalshi_bot import __version__
from kalshi_bot.config import load_settings
from kalshi_bot.core.engine import TradingEngine
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.strategies.examples.market_maker import MarketMaker
from kalshi_bot.strategies.examples.momentum import Momentum

app = typer.Typer(
    add_completion=False,
    help="Kalshi Trading Bot — open-source framework by Viprasol Tech.",
)
console = Console()

STRATEGIES = {"market_maker": MarketMaker, "momentum": Momentum}


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"kalshi-trading-bot [bold cyan]{__version__}[/] — by Viprasol Tech")


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


@app.command()
def balance() -> None:
    """Show your portfolio balance (requires credentials)."""

    async def _run() -> None:
        settings = load_settings()
        async with KalshiClient.from_settings(settings) as client:
            if not client.authenticated:
                console.print("[red]No credentials configured. Set KALSHI_API_KEY_ID.[/]")
                raise typer.Exit(code=1)
            bal = await client.get_balance()
            console.print(f"Balance: [bold green]${bal.balance / 100:,.2f}[/]")

    asyncio.run(_run())


@app.command()
def run(
    strategy: str = typer.Argument(..., help=f"One of: {', '.join(STRATEGIES)}"),
    ticker: str = typer.Argument(..., help="Market ticker to trade."),
    ticks: int = typer.Option(5, help="Number of poll cycles before stopping."),
    live: bool = typer.Option(False, "--live", help="Send real orders (default is dry-run)."),
) -> None:
    """Run a strategy live or in dry-run mode."""
    if strategy not in STRATEGIES:
        choices = ", ".join(STRATEGIES)
        console.print(f"[red]Unknown strategy '{strategy}'. Choose from: {choices}[/]")
        raise typer.Exit(code=1)

    async def _run() -> None:
        settings = load_settings()
        # Dry-run unless --live is explicitly passed.
        dry_run = not live
        async with KalshiClient.from_settings(settings) as client:
            engine = TradingEngine(
                client=client,
                strategy=STRATEGIES[strategy](),
                risk=RiskManager(),
                dry_run=dry_run,
            )
            await engine.run(ticker, max_ticks=ticks)

    asyncio.run(_run())


if __name__ == "__main__":
    app()
