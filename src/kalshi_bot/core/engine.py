"""Simulation-only market polling engine.

Polls a market, hands the snapshot to a strategy, runs each emitted order
through the persistent paper broker and its risk checks. No exchange order
submission is possible. Missing executable quote metadata fails closed.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import asyncio
import time

from kalshi_bot.exchange.client import KalshiClient
from kalshi_bot.exchange.models import Order, OrderRequest
from kalshi_bot.paper import PaperBroker, PaperQuote
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.safety import require_simulation
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
        paper: PaperBroker | None = None,
    ) -> None:
        require_simulation(dry_run=dry_run)
        self.client = client
        self.strategy = strategy
        self.risk = risk
        self.dry_run = dry_run
        self.poll_interval = poll_interval
        self._running = False
        self.paper = paper or PaperBroker(risk=risk)

    async def _snapshot(self, ticker: str) -> StrategyContext:
        market = await self.client.get_market(ticker)
        return StrategyContext(
            market=market,
            positions=self.paper.positions(),
            balance=self.paper.account()["available_cash"],
        )

    async def _submit(self, order: OrderRequest, current_position: int) -> Order | None:
        require_simulation(dry_run=self.dry_run)
        oid = self.paper.submit(order)
        logger.info("[PAPER] order=%s accepted=%s", order.to_payload(), oid is not None)
        return None

    async def run(self, ticker: str, max_ticks: int | None = None) -> None:
        """Run the engine on ``ticker`` until stopped or ``max_ticks`` reached."""
        self._running = True
        logger.info(
            "Engine started | strategy=%s | ticker=%s | dry_run=%s",
            self.strategy.name,
            ticker,
            self.dry_run,
        )
        tick = 0
        try:
            self.strategy.on_start()
            while self._running:
                ctx = await self._snapshot(ticker)
                market = ctx.market
                if market.observed_at is not None and market.quote_id:
                    quote = PaperQuote(**market.model_dump(include=set(PaperQuote.model_fields)))
                    if not self.paper.quote_is_usable(quote, now=time.time()):
                        self.paper.process_quote(quote)
                        tick += 1
                        if max_ticks is not None and tick >= max_ticks:
                            break
                        await asyncio.sleep(self.poll_interval)
                        continue
                    self.paper.process_quote(quote)
                    ctx = StrategyContext(
                        market, self.paper.positions(), self.paper.account()["available_cash"]
                    )
                else:
                    self.paper.record_event(
                        "quote_skipped", {"ticker": ticker, "reason": "missing quote metadata"}
                    )
                    tick += 1
                    if max_ticks is not None and tick >= max_ticks:
                        break
                    await asyncio.sleep(self.poll_interval)
                    continue
                for order in self.strategy.on_market_data(ctx):
                    self.paper.record_event("signal", {"order": order.to_payload()})
                    await self._submit(order, ctx.position_for(order.ticker))
                tick += 1
                if max_ticks is not None and tick >= max_ticks:
                    break
                await asyncio.sleep(self.poll_interval)
        finally:
            try:
                self.paper.cancel_market(ticker, now=time.time())
            finally:
                try:
                    self.strategy.on_stop()
                finally:
                    self._running = False
                    logger.info("Engine stopped after %d tick(s)", tick)

    def stop(self) -> None:
        """Signal the engine to stop after the current tick."""
        self._running = False
