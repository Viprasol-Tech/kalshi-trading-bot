"""Engine and backtester must route through paper execution, never exchange writes."""

import time
from unittest.mock import AsyncMock

import pytest

from kalshi_bot.backtesting.data import random_walk_snapshots
from kalshi_bot.backtesting.engine import Backtester, BacktestReport
from kalshi_bot.core.engine import TradingEngine
from kalshi_bot.exchange.models import Action, Market, OrderRequest, Side
from kalshi_bot.paper import PaperBroker
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.strategies.base import Strategy, StrategyContext
from kalshi_bot.strategies.examples.market_maker import MarketMaker
from kalshi_bot.strategies.examples.momentum import Momentum

pytestmark = pytest.mark.usefixtures("offline_execution_fixture")


class AlwaysBuy(Strategy):
    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        return [
            OrderRequest(
                ticker=ctx.market.ticker,
                side=Side.YES,
                action=Action.BUY,
                count=1,
                yes_price=40,
            )
        ]


def test_unchanged_market_maker_has_no_artificial_profit() -> None:
    snapshots = [
        Market(
            ticker="T",
            last_price=50,
            yes_bid=49,
            yes_ask=51,
            observed_at=1000.0 + i,
            quote_id=f"q{i}",
            yes_bid_size=100,
            yes_ask_size=100,
        )
        for i in range(10)
    ]
    report = Backtester().run(MarketMaker(), snapshots)
    assert report.trades == 0
    assert report.pnl_cents == 0


def test_missing_liquidity_cannot_fill() -> None:
    report = Backtester().run(
        AlwaysBuy(), [Market(ticker="T", last_price=30, yes_bid=29, yes_ask=30)] * 10
    )
    assert report.trades == 0


def test_initial_capital_included_in_return_and_drawdown() -> None:
    report = BacktestReport(
        starting_balance_cents=5000, final_equity_cents=4990, equity_curve=[4990]
    )
    assert report.total_return == -0.002
    assert report.max_drawdown == 0.002


def test_paper_backtest_has_no_instant_fills() -> None:
    snapshots = random_walk_snapshots(n=1)
    assert Backtester().run(AlwaysBuy(), snapshots).trades == 0
    report = Backtester().run(Momentum(), random_walk_snapshots(n=200))
    assert report.starting_balance_cents == 5000
    assert report.fees_paid_cents == sum(f.count for f in report.fills) * 2


@pytest.mark.asyncio
async def test_engine_uses_ledger_and_cancels_pending_on_stop() -> None:
    client = AsyncMock()
    now = time.time()
    client.get_market.return_value = Market(
        ticker="T",
        last_price=38,
        yes_bid=37,
        yes_ask=38,
        observed_at=now,
        quote_id="engine-q",
        yes_bid_size=8,
        yes_ask_size=8,
    )
    broker = PaperBroker(":memory:")
    try:
        engine = TradingEngine(client, AlwaysBuy(), RiskManager(), paper=broker)
        await engine.run("T", max_ticks=1)
        client.create_order.assert_not_called()
        client.get_positions.assert_not_called()
        client.get_balance.assert_not_called()
        assert broker.rows("fills") == []
        assert broker.rows("orders")[0]["status"] == "cancelled"
        assert broker.account()["available_cash"] == 5000
    finally:
        broker.close()


@pytest.mark.asyncio
async def test_engine_skips_stale_market_signals() -> None:
    client = AsyncMock()
    client.get_market.return_value = Market(
        ticker="T",
        observed_at=time.time() - 60,
        quote_id="stale",
        yes_bid=37,
        yes_ask=38,
        yes_bid_size=8,
        yes_ask_size=8,
    )
    broker = PaperBroker(":memory:")
    try:
        engine = TradingEngine(client, AlwaysBuy(), RiskManager(), paper=broker)
        await engine.run("T", max_ticks=1)
        assert broker.rows("orders") == []
        client.create_order.assert_not_called()
    finally:
        broker.close()
