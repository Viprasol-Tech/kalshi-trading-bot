"""Tests for the bundled example strategies."""

from __future__ import annotations

from kalshi_bot.exchange.models import Action, Market, Position, Side
from kalshi_bot.strategies.base import StrategyContext
from kalshi_bot.strategies.examples.arbitrage import ArbitrageYesNo
from kalshi_bot.strategies.examples.fair_value import FairValue
from kalshi_bot.strategies.examples.market_maker import MarketMaker
from kalshi_bot.strategies.examples.mean_reversion import MeanReversion
from kalshi_bot.strategies.examples.momentum import Momentum


def _ctx(market: Market, position: int = 0, balance: int = 100_000) -> StrategyContext:
    return StrategyContext(
        market=market,
        positions={market.ticker: Position(ticker=market.ticker, position=position)},
        balance=balance,
    )


def test_market_maker_quotes_both_sides() -> None:
    mm = MarketMaker(spread_cents=2, quote_size=1, max_inventory=50)
    orders = mm.on_market_data(_ctx(Market(ticker="T", yes_bid=40, yes_ask=44)))
    assert len(orders) == 2
    assert {o.action for o in orders} == {Action.BUY, Action.SELL}


def test_market_maker_stops_buying_at_inventory_cap() -> None:
    mm = MarketMaker(max_inventory=10)
    orders = mm.on_market_data(_ctx(Market(ticker="T", yes_bid=40, yes_ask=44), position=10))
    assert all(o.action is not Action.BUY for o in orders)


def test_momentum_enters_long_on_uptrend() -> None:
    strat = Momentum(window=3, size=2)
    last = []
    for p in (40, 41, 42, 80):
        last = strat.on_market_data(
            _ctx(Market(ticker="T", last_price=p, yes_bid=p - 1, yes_ask=p + 1))
        )
    assert last and last[0].action is Action.BUY
    assert last[0].count == 2


def test_mean_reversion_buys_when_cheap() -> None:
    strat = MeanReversion(window=5, z=1.0, size=1)
    orders = []
    # Stable around 50, then a sharp drop should trigger a buy.
    for p in (50, 50, 51, 49, 50, 30):
        m = Market(ticker="T", last_price=p, yes_bid=p - 1, yes_ask=p + 1)
        orders = strat.on_market_data(_ctx(m))
    assert orders and orders[0].action is Action.BUY
    assert orders[0].side is Side.YES


def test_mean_reversion_warms_up_silently() -> None:
    strat = MeanReversion(window=5)
    m = Market(ticker="T", last_price=50, yes_bid=49, yes_ask=51)
    assert strat.on_market_data(_ctx(m)) == []


def test_arbitrage_buys_both_sides_when_underpriced() -> None:
    strat = ArbitrageYesNo(size=2, min_edge_cents=2)
    # yes_ask 40 + no_ask 55 = 95 -> 5c edge.
    m = Market(ticker="T", yes_ask=40, no_ask=55)
    orders = strat.on_market_data(_ctx(m))
    assert len(orders) == 2
    assert {o.side for o in orders} == {Side.YES, Side.NO}


def test_arbitrage_skips_when_no_edge() -> None:
    strat = ArbitrageYesNo(min_edge_cents=5)
    m = Market(ticker="T", yes_ask=48, no_ask=50)  # sum 98 -> 2c < 5c
    assert strat.on_market_data(_ctx(m)) == []


def test_fair_value_buys_below_fair() -> None:
    strat = FairValue(fair_prob=0.7, margin_cents=3, max_position=50)
    m = Market(ticker="T", yes_ask=40)  # 40 < 70 - 3
    orders = strat.on_market_data(_ctx(m))
    assert orders and orders[0].action is Action.BUY
    assert orders[0].count >= 1


def test_fair_value_skips_when_rich() -> None:
    strat = FairValue(fair_prob=0.5, margin_cents=3)
    m = Market(ticker="T", yes_ask=60)  # above fair value
    assert strat.on_market_data(_ctx(m)) == []
