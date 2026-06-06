"""Tests for the exchange data models."""

from __future__ import annotations

from kalshi_bot.exchange.models import (
    Action,
    Balance,
    Fill,
    OrderBook,
    OrderBookLevel,
    OrderRequest,
    OrderType,
    Portfolio,
    Position,
    Side,
)


def test_order_request_omits_unset_fields() -> None:
    payload = OrderRequest(
        ticker="T", action=Action.BUY, side=Side.YES, count=2, type=OrderType.LIMIT, yes_price=40
    ).to_payload()
    assert payload["ticker"] == "T"
    assert "no_price" not in payload
    assert payload["yes_price"] == 40


def test_position_flat_and_unrealized() -> None:
    pos = Position(ticker="T", position=10, avg_price=40.0)
    assert not pos.is_flat
    assert pos.unrealized_pnl(mark_price=45) == 50  # (45-40)*10
    assert Position(ticker="T").is_flat


def test_fill_helpers() -> None:
    buy = Fill(ticker="T", side=Side.YES, action=Action.BUY, count=3, price=30)
    sell = Fill(ticker="T", side=Side.YES, action=Action.SELL, count=2, price=60)
    assert buy.notional_cents == 90
    assert buy.signed_count == 3
    assert sell.signed_count == -2


def test_orderbook_best_prices_and_mid() -> None:
    book = OrderBook(
        ticker="T",
        yes=[OrderBookLevel(price=40, count=5), OrderBookLevel(price=42, count=3)],
        no=[OrderBookLevel(price=55, count=4)],  # YES ask = 100 - 55 = 45
    )
    assert book.best_yes_bid == 42
    assert book.best_yes_ask == 45
    assert book.mid_price == 43.5
    assert book.spread == 3


def test_orderbook_empty_sides() -> None:
    book = OrderBook(ticker="T")
    assert book.best_yes_bid is None
    assert book.best_yes_ask is None
    assert book.mid_price is None
    assert book.spread is None


def test_balance_dollars() -> None:
    assert Balance(balance=12_345).balance_dollars == 123.45


def test_portfolio_lookup_and_value() -> None:
    pf = Portfolio(
        balance_cents=10_000,
        positions=[Position(ticker="A", position=10), Position(ticker="B", position=-5)],
    )
    assert pf.position_for("A") is not None
    assert pf.position_for("Z") is None
    # 10000 + 10*50 + (-5)*30 = 10000 + 500 - 150
    assert pf.market_value_cents({"A": 50, "B": 30}) == 10_350
