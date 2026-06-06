"""Synthetic market-data generators for backtests and demos.

These let you exercise a strategy and the metrics pipeline without any network
access or recorded data — useful for the CLI ``backtest`` demo, tests, and
quick experimentation. The generated prices are a deterministic random walk
clamped to the valid 1-99 cent range.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import random

from kalshi_bot.exchange.models import Market


def random_walk_snapshots(
    ticker: str = "DEMO-MARKET",
    n: int = 200,
    start: int = 50,
    step: int = 2,
    spread: int = 1,
    seed: int | None = 42,
) -> list[Market]:
    """Generate ``n`` :class:`Market` snapshots following a clamped random walk.

    Args:
        ticker: Market ticker stamped on every snapshot.
        n: Number of snapshots to produce.
        start: Starting last price in cents (clamped to 1-99).
        step: Maximum absolute price change per step.
        spread: Half-spread used to derive bid/ask around the last price.
        seed: RNG seed for reproducibility; pass ``None`` for fresh randomness.
    """
    rng = random.Random(seed)
    price = max(1, min(99, start))
    out: list[Market] = []
    for _ in range(max(0, n)):
        price = max(2, min(98, price + rng.randint(-step, step)))
        bid = max(1, price - spread)
        ask = min(99, price + spread)
        out.append(
            Market(
                ticker=ticker,
                last_price=price,
                yes_bid=bid,
                yes_ask=ask,
                no_bid=100 - ask,
                no_ask=100 - bid,
            )
        )
    return out
