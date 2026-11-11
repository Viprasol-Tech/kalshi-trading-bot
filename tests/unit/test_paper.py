"""Cash, reservations, conservative execution, persistence and settlement."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from kalshi_bot.exchange.models import Action, OrderRequest, Side, TimeInForce
from kalshi_bot.paper import PaperBroker, PaperQuote
from kalshi_bot.risk.manager import RiskLimits, RiskManager
from kalshi_bot.safety import SimulationOnlyError


def buy(
    price: int = 40,
    count: int = 2,
    side: Side = Side.YES,
    **extra: object,
) -> OrderRequest:
    return OrderRequest(
        ticker="T",
        side=side,
        action=Action.BUY,
        count=count,
        **({"yes_price": price} if side is Side.YES else {"no_price": price}),
        **extra,
    )


def quote(t: float = 101, size: int = 4, **changes: object) -> PaperQuote:
    data: dict[str, object] = {
        "quote_id": f"q-{t}",
        "ticker": "T",
        "observed_at": t,
        "yes_bid": 38,
        "yes_ask": 39,
        "yes_bid_size": size,
        "yes_ask_size": size,
        "no_bid": 61,
        "no_ask": 62,
        "no_bid_size": size,
        "no_ask_size": size,
    }
    data.update(changes)
    return PaperQuote.model_validate(data)


@pytest.fixture
def broker() -> Iterator[PaperBroker]:
    value = PaperBroker(":memory:")
    yield value
    value.close()


def test_submission_reserves_but_never_fills(broker: PaperBroker) -> None:
    assert broker.submit(buy(), now=100)
    assert broker.account()["cash"] == 5000
    assert broker.account()["reserved_cash"] == 84
    assert broker.account()["available_cash"] == 4916
    assert broker.rows("fills") == []


def test_partial_fills_share_liquidity_and_preserve_reserves(broker: PaperBroker) -> None:
    first = broker.submit(buy(count=2), now=100)
    second = broker.submit(buy(count=1), now=100)
    assert broker.process_quote(quote(size=4), now=101) == 1
    assert [r["quantity"] for r in broker.rows("fills")] == [1]
    assert broker.account()["cash"] == 4958
    assert broker.account()["reserved_cash"] == 84
    assert broker.rows("orders")[0]["remaining"] == 1
    assert broker.process_quote(quote(102, size=4), now=102) == 1
    broker.cancel(first, now=103)  # type: ignore[arg-type]
    broker.cancel(second, now=103)  # type: ignore[arg-type]
    assert broker.account()["reserved_cash"] == 0
    assert broker.holdings("T", Side.YES) == 2


def test_fifo_realized_pnl_includes_entry_and_exit_fees(broker: PaperBroker) -> None:
    broker.submit(buy(), now=100)
    broker.process_quote(quote(), now=101)  # one at 40 + 2 fee
    broker.process_quote(quote(102, yes_ask=38, yes_bid=37), now=102)  # one at 39 + 2
    sell = OrderRequest(ticker="T", action=Action.SELL, side=Side.YES, count=2, yes_price=50)
    assert broker.submit(sell, now=102)
    assert broker.process_quote(quote(103, size=8, yes_bid=52, yes_ask=54), now=103) == 2
    assert broker.account()["cash"] == 5015
    assert broker.account()["realized_pnl"] == 15
    assert broker.account()["fees"] == 8
    assert broker.holdings("T", Side.YES) == 0


@pytest.mark.parametrize(
    "changes,now",
    [
        ({}, 107),  # more than five seconds old
        ({"observed_at": 108}, 107),  # future
        ({"yes_ask": None}, 101),  # missing quote
        ({"yes_ask_size": 0}, 101),  # no depth
        ({"yes_ask_size": 3}, 101),  # participation rounds down
        ({"yes_ask": 40}, 101),  # slippage exceeds limit
    ],
)
def test_conservative_no_fills(
    broker: PaperBroker,
    changes: dict[str, object],
    now: float,
) -> None:
    broker.submit(buy(), now=100)
    assert broker.process_quote(quote(**changes), now=now) == 0
    assert broker.rows("fills") == []


def test_same_timestamp_and_replay_cannot_fill(broker: PaperBroker) -> None:
    broker.submit(buy(), now=101)
    assert broker.process_quote(quote(), now=101) == 0
    assert broker.process_quote(quote(), now=102) == 0
    assert broker.process_quote(quote(100), now=102) == 0


def test_post_only_is_not_assumed_to_fill(broker: PaperBroker) -> None:
    broker.submit(buy(post_only=True), now=100)
    assert broker.process_quote(quote(size=40), now=101) == 0


def test_ioc_cancels_unfilled_remainder(broker: PaperBroker) -> None:
    broker.submit(buy(time_in_force=TimeInForce.IMMEDIATE_OR_CANCEL), now=100)
    assert broker.process_quote(quote(), now=101) == 1
    assert broker.pending() == []
    assert broker.account()["reserved_cash"] == 0


def test_max_loss_cap_includes_fees(broker: PaperBroker) -> None:
    assert broker.submit(buy(price=49, count=2), now=100) is None  # 102 cents
    assert broker.submit(buy(price=98, count=1), now=100) is not None  # exactly $1
    assert broker.process_quote(quote(101, size=4, yes_ask=97, yes_bid=96), now=101) == 1
    broker.settle("T", 0, source="synthetic-test", confirmed=True, now=102)
    assert broker.account()["realized_pnl"] == -100
    assert broker.account()["cash"] == 4900


def test_insufficient_reserved_cash_and_duplicate_ids() -> None:
    broker = PaperBroker(":memory:", starting_cash=100)
    try:
        assert broker.submit(buy(client_order_id="entry"), now=100)
        assert broker.submit(buy(client_order_id="entry"), now=100) is None
        assert broker.submit(buy(), now=100) is None
        assert broker.account()["available_cash"] == 16
    finally:
        broker.close()


def test_pending_exposure_limit() -> None:
    broker = PaperBroker(":memory:", risk=RiskManager(RiskLimits(max_position_per_market=2)))
    try:
        assert broker.submit(buy(), now=100)
        assert broker.submit(buy(count=1), now=100) is None
        assert broker.submit(buy(side=Side.NO, count=2), now=100)
        assert broker.submit(buy(side=Side.NO, count=1), now=100) is None
    finally:
        broker.close()


def test_no_holdings_cannot_sell(broker: PaperBroker) -> None:
    sell = OrderRequest(ticker="T", side=Side.YES, action=Action.SELL, count=1, yes_price=50)
    assert broker.submit(sell, now=100) is None


def test_sell_inventory_reserved_once(broker: PaperBroker) -> None:
    broker.submit(buy(count=1), now=100)
    broker.process_quote(quote(), now=101)
    sell = OrderRequest(ticker="T", side=Side.YES, action=Action.SELL, count=1, yes_price=50)
    assert broker.submit(sell, now=102)
    assert broker.submit(sell, now=102) is None


def test_no_side_cash_and_settlement_are_correct(broker: PaperBroker) -> None:
    broker.submit(buy(price=63, count=1, side=Side.NO), now=100)
    assert broker.process_quote(quote(), now=101) == 1  # 62 ask + 1 slippage + 2 fee
    assert broker.positions()["T"].position == -1
    assert broker.account()["cash"] == 4935
    assert broker.settle("T", 0, source="synthetic-result", confirmed=True, now=102) == 100
    assert broker.account()["cash"] == 5035
    assert broker.account()["realized_pnl"] == 35
    assert broker.settle("T", 0, source="synthetic-result", confirmed=True, now=103) == 0
    with pytest.raises(ValueError):
        broker.settle("T", 100, source="conflict", confirmed=True)
    assert broker.submit(buy(), now=104) is None
    assert broker.positions() == {}


def test_unconfirmed_settlement_fails_closed(broker: PaperBroker) -> None:
    with pytest.raises(ValueError):
        broker.settle("T", 100, source="unverified")
    with pytest.raises(ValueError):
        broker.settle("T", 50, source="non-binary", confirmed=True)
    assert broker.rows("settlements") == []


def test_restart_does_not_reset_account_or_pending_orders(tmp_path: Path) -> None:
    path = tmp_path / "paper.db"
    first = PaperBroker(path)
    first.submit(buy(), now=100)
    first.process_quote(quote(), now=101)
    before = first.account()
    first.close()
    second = PaperBroker(path)
    try:
        assert second.account() == before
        assert len(second.pending()) == 1
        assert second.holdings("T", Side.YES) == 1
        assert second.process_quote(quote(), now=102) == 0
        assert len(second.rows("fills")) == 1
    finally:
        second.close()


def test_two_connections_cannot_double_spend(tmp_path: Path) -> None:
    path = tmp_path / "shared.db"
    first = PaperBroker(path, starting_cash=100)
    second = PaperBroker(path, starting_cash=100)
    try:
        assert first.submit(buy(), now=100)
        assert second.submit(buy(), now=100) is None
        assert second.account()["available_cash"] == 16
    finally:
        first.close()
        second.close()


def test_unexpected_fill_error_rolls_back(
    broker: PaperBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    broker.submit(buy(), now=100)
    before = broker.account()

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("injected failure")

    monkeypatch.setattr(broker, "_fill", fail)
    with pytest.raises(RuntimeError):
        broker.process_quote(quote(), now=101)
    assert broker.account() == before
    assert broker.rows("fills") == []
    assert broker.quote_is_usable(quote(), now=101)


def test_environment_override_blocks_paper_execution(
    broker: PaperBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KALSHI_ENVIRONMENT", "prod")
    with pytest.raises(SimulationOnlyError):
        broker.submit(buy(), now=100)


def test_complementary_liquidity_is_not_double_counted(broker: PaperBroker) -> None:
    broker.submit(buy(price=63, count=1, side=Side.NO), now=99)
    broker.process_quote(quote(100), now=100)
    broker.submit(buy(count=1), now=101)
    sell_no = OrderRequest(ticker="T", action=Action.SELL, side=Side.NO, count=1, no_price=60)
    broker.submit(sell_no, now=101)
    assert broker.process_quote(quote(102), now=102) == 1
    assert broker.holdings("T", Side.NO) == 1
    assert broker.holdings("T", Side.YES) == 1


def test_zero_balance_after_loss_cannot_submit() -> None:
    broker = PaperBroker(":memory:", starting_cash=100)
    try:
        broker.submit(buy(price=98, count=1), now=100)
        broker.process_quote(quote(101, yes_ask=97, yes_bid=96), now=101)
        assert broker.account()["available_cash"] == 0
        assert broker.submit(buy(count=1), now=102) is None
    finally:
        broker.close()


def test_rejects_unsupported_fok(broker: PaperBroker) -> None:
    assert broker.submit(buy(time_in_force=TimeInForce.FILL_OR_KILL), now=100) is None
    assert broker.rows("orders") == []


def test_cannot_refund_existing_ledger(tmp_path: Path) -> None:
    path = tmp_path / "fixed.db"
    first = PaperBroker(path, starting_cash=100)
    first.close()
    with pytest.raises(ValueError):
        PaperBroker(path, starting_cash=5000)
