"""Tests for the upgraded backtester (fills, PnL, metrics, report)."""

from __future__ import annotations

from kalshi_bot.backtesting.data import random_walk_snapshots
from kalshi_bot.backtesting.engine import Backtester, _apply_fill
from kalshi_bot.backtesting.report import build_table
from kalshi_bot.exchange.models import Action, Market, OrderRequest, OrderType, Side
from kalshi_bot.risk.manager import RiskLimits, RiskManager
from kalshi_bot.strategies.base import Strategy, StrategyContext
from kalshi_bot.strategies.examples.momentum import Momentum


class _BuyThenSell(Strategy):
    """Buy on the first tick at 40c, sell on a later tick at 60c (a winner)."""

    name = "buy_then_sell"

    def __init__(self) -> None:
        super().__init__()
        self._step = 0

    def on_market_data(self, ctx: StrategyContext) -> list[OrderRequest]:
        self._step += 1
        t = ctx.market.ticker
        if self._step == 1:
            return [
                OrderRequest(
                    ticker=t,
                    action=Action.BUY,
                    side=Side.YES,
                    count=10,
                    type=OrderType.LIMIT,
                    yes_price=40,
                )
            ]
        if self._step == 3:
            return [
                OrderRequest(
                    ticker=t,
                    action=Action.SELL,
                    side=Side.YES,
                    count=10,
                    type=OrderType.LIMIT,
                    yes_price=60,
                )
            ]
        return []


def _flat_snapshots() -> list[Market]:
    return [Market(ticker="T", last_price=50, yes_bid=49, yes_ask=51) for _ in range(4)]


def test_apply_fill_opens_and_blends() -> None:
    pos, avg, realized = _apply_fill(0, 0.0, 10, 40)
    assert pos == 10 and avg == 40.0 and realized is None
    pos, avg, realized = _apply_fill(pos, avg, 10, 50)
    assert pos == 20 and avg == 45.0 and realized is None


def test_apply_fill_realizes_on_close() -> None:
    pos, _avg, realized = _apply_fill(10, 40.0, -10, 60)
    assert pos == 0
    assert realized == (60 - 40) * 10  # +200c


def test_apply_fill_flip_through_flat() -> None:
    # Long 5 @ 40, sell 8 @ 60: closes 5 (realize), opens short 3 @ 60.
    pos, avg, realized = _apply_fill(5, 40.0, -8, 60)
    assert pos == -3
    assert avg == 60.0
    assert realized == (60 - 40) * 5


def test_backtester_realizes_winning_trade() -> None:
    bt = Backtester(starting_balance_cents=100_000)
    report = bt.run(_BuyThenSell(), _flat_snapshots())
    assert report.trades == 2
    assert report.final_position == 0
    assert report.trade_pnls == [200.0]
    assert report.win_rate == 1.0
    assert report.pnl_cents == 200


def test_backtester_records_equity_curve_length() -> None:
    snaps = random_walk_snapshots(n=50, seed=1)
    report = Backtester().run(Momentum(window=5), snaps)
    assert len(report.equity_curve) == 50
    assert "sharpe" in report.summary()


def test_backtester_respects_risk_manager() -> None:
    # Order of 10 exceeds a 1-contract cap, so no trades execute.
    rm = RiskManager(RiskLimits(max_contracts_per_order=1))
    report = Backtester(risk=rm).run(_BuyThenSell(), _flat_snapshots())
    assert report.trades == 0


def test_backtester_charges_fees() -> None:
    bt = Backtester(fee_cents_per_contract=1)
    report = bt.run(_BuyThenSell(), _flat_snapshots())
    assert report.fees_paid_cents == 20  # 10 contracts * 2 fills * 1c


def test_report_table_renders() -> None:
    report = Backtester().run(Momentum(window=5), random_walk_snapshots(n=30, seed=2))
    table = build_table(report)
    assert table.row_count >= 8
