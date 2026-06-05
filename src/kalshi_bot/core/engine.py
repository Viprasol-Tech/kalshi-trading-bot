"""The live trading engine.

Polls a market, hands the snapshot to a strategy, runs each emitted order
through the :class:`~kalshi_bot.risk.manager.RiskManager`, and either submits it
(live) or logs it (dry-run). Dry-run is the default and is strongly recommended
until you fully understand a strategy.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import asyncio

from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.exchange.models import Order, OrderRequest, Position
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.strategies.base import Strategy, StrategyContext
from kalshi_bot.telemetry.logging import get_logger

logger = get_logger(__name__)


class TradingEngine:
    """Drives a single strategy against a single market."""

    def __init__(
        self,
        client: KalshiClient,
        strategy: Strategy,
        risk: RiskManager,
        dry_run: bool = True,
        poll_interval: float = 2.0,
    ) -> None:
        self.client = client
        self.strategy = strategy
        self.risk = risk
        self.dry_run = dry_run
        self.poll_interval = poll_interval
        self._running = False

    async def _snapshot(self, ticker: str) -> StrategyContext:
        market = await self.client.get_market(ticker)
        positions: dict[str, Position] = {}
        balance = 0
        if self.client.authenticated:
            for pos in await self.client.get_positions():
                positions[pos.ticker] = pos
            balance = (await self.client.get_balance()).balance
        return StrategyContext(market=market, positions=positions, balance=balance)

    async def _submit(self, order: OrderRequest, current_position: int) -> Order | None:
        decision = self.risk.check(order, current_position)
        if not decision.approved:
            logger.warning("Order vetoed by risk manager: %s | %s", decision.reason, order)
            return None
        if self.dry_run:
            logger.info("[DRY-RUN] would submit: %s", order.to_payload())
            return None
        order_resp = await self.client.create_order(order)
        logger.info("Submitted order %s for %s", order_resp.order_id, order.ticker)
        return order_resp

    async def run(self, ticker: str, max_ticks: int | None = None) -> None:
        """Run the engine on ``ticker`` until stopped or ``max_ticks`` reached."""
        self._running = True
        self.strategy.on_start()
        logger.info(
            "Engine started | strategy=%s | ticker=%s | dry_run=%s",
            self.strategy.name,
            ticker,
            self.dry_run,
        )
        tick = 0
        try:
            while self._running:
                ctx = await self._snapshot(ticker)
                for order in self.strategy.on_market_data(ctx):
                    await self._submit(order, ctx.position_for(order.ticker))
                tick += 1
                if max_ticks is not None and tick >= max_ticks:
                    break
                await asyncio.sleep(self.poll_interval)
        finally:
            self.strategy.on_stop()
            self._running = False
            logger.info("Engine stopped after %d tick(s)", tick)

    def stop(self) -> None:
        """Signal the engine to stop after the current tick."""
        self._running = False
