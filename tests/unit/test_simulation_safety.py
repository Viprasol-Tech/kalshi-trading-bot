"""Regression tests for non-bypassable simulation-only policy."""

import httpx
import pytest
import respx
from pydantic import ValidationError
from typer.testing import CliRunner

from kalshi_bot.cli import app
from kalshi_bot.config import Settings
from kalshi_bot.core.engine import TradingEngine
from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.exchange.models import Action, Fill, Market, OrderRequest, Side
from kalshi_bot.risk.manager import RiskLimits, RiskManager
from kalshi_bot.safety import SimulationOnlyError
from kalshi_bot.strategies.base import StrategyContext
from kalshi_bot.strategies.examples.fair_value import FairValue


def order(side: Side = Side.YES, action: Action = Action.BUY, count: int = 2) -> OrderRequest:
    return OrderRequest(
        ticker="T",
        action=action,
        side=side,
        count=count,
        **({"yes_price": 40} if side is Side.YES else {"no_price": 40}),
    )


@pytest.mark.parametrize(
    "side,action,delta",
    [
        (Side.YES, Action.BUY, 2),
        (Side.YES, Action.SELL, -2),
        (Side.NO, Action.BUY, -2),
        (Side.NO, Action.SELL, 2),
    ],
)
def test_signed_directions(side: Side, action: Action, delta: int) -> None:
    request = order(side, action)
    assert request.signed_count == delta
    assert Fill(ticker="T", side=side, action=action, count=2, price=40).signed_count == delta


def test_no_side_and_pending_limits() -> None:
    rm = RiskManager(RiskLimits(max_position_per_market=5))
    assert not rm.check(order(Side.NO), -4, available_cash=5000).approved
    assert not rm.check(
        order(count=3), 0, available_cash=5000, pending_orders=[order(count=3)]
    ).approved
    # Opposite-side pending orders must not conceal unilateral fill exposure.
    assert not rm.check(
        order(count=3),
        0,
        available_cash=5000,
        pending_orders=[order(count=3), order(Side.NO, count=3)],
    ).approved


@pytest.mark.parametrize("cash", [None, 0, -1, 79, 83])
def test_missing_or_insufficient_cash_rejected(cash: int | None) -> None:
    assert not RiskManager().check(order(), 0, available_cash=cash).approved


@pytest.mark.parametrize(
    "changes",
    [
        {"dry_run": False},
        {"environment": "prod"},
        {"api_key_id": "test-placeholder"},
    ],
)
def test_settings_reject_unsafe_options(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **changes)


@pytest.mark.asyncio
async def test_boundary_blocks_writes_even_with_overridden_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with KalshiClient("https://demo-api.kalshi.co/trade-api/v2") as client:
        with pytest.raises(SimulationOnlyError):
            await client.create_order(order())
        with pytest.raises(SimulationOnlyError):
            await client._request("POST", "/portfolio/orders", json=order().to_payload())
        with pytest.raises(SimulationOnlyError):
            await client.cancel_order("test")
        monkeypatch.setenv("KALSHI_DRY_RUN", "false")
        with pytest.raises(SimulationOnlyError):
            await client.get_markets()


def test_live_cli_rejected_before_client_creation() -> None:
    result = CliRunner().invoke(app, ["run", "momentum", "T", "--live"])
    assert result.exit_code == 1
    assert "--live is disabled" in result.stdout


@pytest.mark.parametrize(
    "base",
    [
        "https://demo-api.kalshi.co",
        "https://demo-api.kalshi.co/trade-api/v2",
    ],
)
@pytest.mark.asyncio
async def test_api_prefix_is_not_duplicated(base: str) -> None:
    with respx.mock:
        route = respx.get("https://demo-api.kalshi.co/trade-api/v2/markets").mock(
            return_value=httpx.Response(200, json={"markets": []})
        )
        async with KalshiClient(base) as client:
            assert await client.get_markets() == []
        assert route.call_count == 1


def test_production_and_credentials_rejected() -> None:
    with pytest.raises(SimulationOnlyError):
        KalshiClient("https://api.elections.kalshi.com")
    with pytest.raises(SimulationOnlyError):
        KalshiClient("https://demo-api.kalshi.co", api_key_id="test-placeholder")


def test_fair_value_zero_bankroll() -> None:
    ctx = StrategyContext(Market(ticker="T", yes_ask=40), {}, 0)
    assert FairValue().on_market_data(ctx) == []


def test_engine_live_constructor_rejected() -> None:
    with pytest.raises(SimulationOnlyError):
        TradingEngine(None, FairValue(), RiskManager(), dry_run=False)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_raw_http_client_still_has_a_safety_boundary() -> None:
    async with KalshiClient("https://demo-api.kalshi.co/trade-api/v2") as client:
        with pytest.raises(SimulationOnlyError):
            await client._client.post("/portfolio/orders", json={})
        with pytest.raises(SimulationOnlyError):
            await client._client.get("https://api.elections.kalshi.com/trade-api/v2/markets")
        with pytest.raises(SimulationOnlyError):
            await client._client.get("/markets", headers={"Authorization": "test-placeholder"})


@pytest.mark.parametrize(
    "changes",
    [
        {"yes_price": None},
        {"no_price": 40},
        {"side": Side.NO},
        {"type": "market"},
        {"count": 0},
    ],
)
def test_malformed_orders_rejected(changes: dict[str, object]) -> None:
    data = order().model_dump()
    data.update(changes)
    with pytest.raises(ValidationError):
        OrderRequest.model_validate(data)


def test_authenticated_websockets_are_disabled() -> None:
    from kalshi_bot.exchange.websocket import KalshiWebSocket

    with pytest.raises(SimulationOnlyError):
        KalshiWebSocket.from_settings(Settings(_env_file=None))
