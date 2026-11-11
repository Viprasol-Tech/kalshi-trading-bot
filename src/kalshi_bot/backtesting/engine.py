"""A snapshot-replay backtester for prediction-market strategies.

Replays timestamped executable quotes through a strategy and the conservative
paper broker. Only later quotes can fill funded orders. Tracks an equity curve
and a per-trade PnL log, and produces a rich
:class:`BacktestReport` with standard quant metrics (Sharpe, max drawdown,
win rate). This is intentionally simple — a starting point, not a
high-fidelity matching engine.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_bot.backtesting import metrics
from kalshi_bot.exchange.models import Action, Fill, Market, Side
from kalshi_bot.paper import FEE_PER_CONTRACT, PaperBroker, PaperQuote
from kalshi_bot.risk.manager import RiskManager
from kalshi_bot.strategies.base import Strategy, StrategyContext


@dataclass(slots=True)
class BacktestReport:
    """Full result of a backtest run, including metrics.

    Monetary fields are in integer cents unless suffixed ``_dollars``. The
    equity curve is recorded once per snapshot (cash + mark-to-market inventory).
    """

    trades: int = 0
    starting_balance_cents: int = 0
    final_equity_cents: int = 0
    final_position: int = 0
    fees_paid_cents: int = 0
    closed_pnl_cents: int = 0
    equity_curve: list[int] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    trade_pnls: list[float] = field(default_factory=list)

    @property
    def pnl_cents(self) -> int:
        """Net PnL over the run in cents (final equity minus starting balance)."""
        return self.final_equity_cents - self.starting_balance_cents

    @property
    def pnl_dollars(self) -> float:
        """Net PnL converted to dollars."""
        return self.pnl_cents / 100.0

    @property
    def realized_pnl_cents(self) -> int:
        """FIFO realized PnL after entry and exit fees, excluding open inventory."""
        return self.closed_pnl_cents

    @property
    def realized_pnl_dollars(self) -> float:
        """Realized PnL converted to dollars."""
        return self.realized_pnl_cents / 100.0

    @property
    def total_return(self) -> float:
        """Fractional return over the run."""
        return self.pnl_cents / self.starting_balance_cents if self.starting_balance_cents else 0.0

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe ratio of the equity curve."""
        rets = metrics.returns_from_equity(
            [float(self.starting_balance_cents), *map(float, self.equity_curve)]
        )
        return metrics.sharpe_ratio(rets)

    @property
    def max_drawdown(self) -> float:
        """Worst peak-to-trough fractional decline (non-negative)."""
        return metrics.max_drawdown(
            [float(self.starting_balance_cents), *map(float, self.equity_curve)]
        )

    @property
    def volatility(self) -> float:
        """Annualised return volatility."""
        rets = metrics.returns_from_equity(
            [float(self.starting_balance_cents), *map(float, self.equity_curve)]
        )
        return metrics.volatility(rets)

    @property
    def win_rate(self) -> float:
        """Fraction of closing trades that were profitable."""
        return metrics.win_rate(self.trade_pnls)

    @property
    def profit_factor(self) -> float:
        """Gross profit divided by gross loss across closing trades."""
        return metrics.profit_factor(self.trade_pnls)

    def summary(self) -> dict[str, float]:
        """Return a flat dict of headline metrics (handy for logging/JSON)."""
        return {
            "trades": float(self.trades),
            "pnl_dollars": round(self.pnl_dollars, 2),
            "total_return": round(self.total_return, 4),
            "sharpe": round(self.sharpe, 3),
            "max_drawdown": round(self.max_drawdown, 4),
            "volatility": round(self.volatility, 4),
            "win_rate": round(self.win_rate, 4),
            "profit_factor": round(self.profit_factor, 3),
        }


class Backtester:
    """Replay through the same conservative paper broker used by the engine.

    Bare last-price snapshots have no executable liquidity and cannot fill.
    Each run is an isolated in-memory ledger, never the persistent paper account.
    """

    def __init__(
        self,
        starting_balance_cents: int = 5000,
        risk: RiskManager | None = None,
        fee_cents_per_contract: int = FEE_PER_CONTRACT,
    ) -> None:
        self.starting_balance_cents = starting_balance_cents
        self.risk = risk
        if fee_cents_per_contract != FEE_PER_CONTRACT:
            raise ValueError("Simulation uses a fixed conservative two-cent fee per contract.")
        self.fee_cents_per_contract = fee_cents_per_contract

    def run(self, strategy: Strategy, snapshots: list[Market]) -> BacktestReport:
        """Run ``strategy`` over ``snapshots`` and return a full report."""
        report = BacktestReport(starting_balance_cents=self.starting_balance_cents)
        broker = PaperBroker(":memory:", starting_cash=self.starting_balance_cents, risk=self.risk)
        marks: dict[str, PaperQuote] = {}
        try:
            strategy.on_start()
            for market in snapshots:
                if market.observed_at is None or not market.quote_id:
                    broker.record_event("quote_skipped", {"reason": "missing quote metadata"})
                    report.equity_curve.append(broker.equity(marks))
                    continue
                quote = PaperQuote(**market.model_dump(include=set(PaperQuote.model_fields)))
                if not broker.quote_is_usable(quote, now=market.observed_at):
                    broker.process_quote(quote, now=market.observed_at)
                    report.equity_curve.append(broker.equity(marks))
                    continue
                broker.process_quote(quote, now=market.observed_at)
                marks[market.ticker] = quote
                ctx = StrategyContext(
                    market, broker.positions(), broker.account()["available_cash"]
                )
                for order in strategy.on_market_data(ctx):
                    broker.record_event("signal", {"order": order.to_payload()})
                    broker.submit(order, now=market.observed_at)
                report.equity_curve.append(broker.equity(marks))
            report.final_equity_cents = (
                report.equity_curve[-1] if report.equity_curve else broker.account()["cash"]
            )
            report.final_position = sum(p.position for p in broker.positions().values())
            report.fees_paid_cents = broker.account()["fees"]
            report.closed_pnl_cents = broker.account()["realized_pnl"]
            for row in broker.rows("fills"):
                report.fills.append(
                    Fill(
                        ticker=row["ticker"],
                        action=Action(row["action"]),
                        side=Side(row["side"]),
                        count=row["quantity"],
                        price=row["price"],
                        order_id=row["order_id"],
                    )
                )
            report.trades = len(report.fills)
            # FIFO closed PnL includes both entry and exit fees.
            cost_lots: dict[tuple[str, str], list[list[int]]] = {}
            for row in broker.rows("fills"):
                key = row["ticker"], row["side"]
                if row["action"] == "buy":
                    cost_lots.setdefault(key, []).append(
                        [row["quantity"], row["price"] + FEE_PER_CONTRACT]
                    )
                else:
                    remaining, cost = row["quantity"], 0
                    for lot in cost_lots[key]:
                        take = min(remaining, lot[0])
                        cost += take * lot[1]
                        remaining -= take
                        lot[0] -= take
                    report.trade_pnls.append(row["quantity"] * row["price"] - row["fee"] - cost)
            return report
        finally:
            try:
                strategy.on_stop()
            finally:
                broker.close()


# Backwards-compatible alias: the report grew metrics but kept the old name's role.
BacktestResult = BacktestReport


def _apply_fill(
    position: int, avg_price: float, qty: int, price: int
) -> tuple[int, float, float | None]:
    """Update position/VWAP for a fill and return realised PnL if it closes any.

    Returns ``(new_position, new_avg_price, realized_pnl_or_None)``. A trade that
    only adds to inventory realises nothing (``None``); one that reduces or flips
    realises PnL on the closed contracts in cents.
    """
    new_position = position + qty
    same_direction = position == 0 or (position > 0) == (qty > 0)

    if same_direction:
        # Adding to (or opening) inventory: blend the VWAP, realise nothing.
        total = position + qty
        new_avg = (avg_price * position + price * qty) / total if total != 0 else 0.0
        return new_position, new_avg, None

    # Reducing or flipping: realise PnL on the contracts that close.
    closed = min(abs(qty), abs(position))
    direction = 1 if position > 0 else -1
    realized = (price - avg_price) * closed * direction

    if abs(qty) <= abs(position):
        new_avg = avg_price if new_position != 0 else 0.0
    else:
        # Flipped past flat: remaining contracts open a fresh position at price.
        new_avg = float(price)
    return new_position, new_avg, realized
