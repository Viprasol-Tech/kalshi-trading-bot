"""A snapshot-replay backtester for prediction-market strategies.

Replays a sequence of :class:`~kalshi_bot.exchange.models.Market` snapshots
through a strategy, simulating immediate fills at the quoted price, tracks an
equity curve and a per-trade PnL log, and produces a rich
:class:`BacktestReport` with standard quant metrics (Sharpe, max drawdown,
win rate). This is intentionally simple — a starting point, not a
high-fidelity matching engine.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kalshi_bot.backtesting import metrics
from kalshi_bot.exchange.models import Action, Fill, Market, Position
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
        """Alias for :attr:`pnl_cents` (kept for 0.1.x compatibility)."""
        return self.pnl_cents

    @property
    def realized_pnl_dollars(self) -> float:
        """Alias for :attr:`pnl_dollars` (kept for 0.1.x compatibility)."""
        return self.pnl_dollars

    @property
    def total_return(self) -> float:
        """Fractional return over the run."""
        return metrics.total_return([float(x) for x in self.equity_curve])

    @property
    def sharpe(self) -> float:
        """Annualised Sharpe ratio of the equity curve."""
        rets = metrics.returns_from_equity([float(x) for x in self.equity_curve])
        return metrics.sharpe_ratio(rets)

    @property
    def max_drawdown(self) -> float:
        """Worst peak-to-trough fractional decline (non-negative)."""
        return metrics.max_drawdown([float(x) for x in self.equity_curve])

    @property
    def volatility(self) -> float:
        """Annualised return volatility."""
        rets = metrics.returns_from_equity([float(x) for x in self.equity_curve])
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
    """Replay market snapshots through a strategy with naive fills.

    Each emitted order is optionally screened by a :class:`RiskManager` and, if
    approved, filled instantly at its limit price. Position cost basis is tracked
    so that closing (or reducing) trades realise a per-trade PnL, which feeds the
    win-rate and profit-factor metrics.
    """

    def __init__(
        self,
        starting_balance_cents: int = 100_000,
        risk: RiskManager | None = None,
        fee_cents_per_contract: int = 0,
    ) -> None:
        self.starting_balance_cents = starting_balance_cents
        self.risk = risk
        self.fee_cents_per_contract = max(0, fee_cents_per_contract)

    def run(self, strategy: Strategy, snapshots: list[Market]) -> BacktestReport:
        """Run ``strategy`` over ``snapshots`` and return a full report."""
        report = BacktestReport(starting_balance_cents=self.starting_balance_cents)
        position = 0
        avg_price = 0.0  # VWAP of current inventory (YES-price cents)
        balance = float(self.starting_balance_cents)

        strategy.on_start()
        for market in snapshots:
            ctx = StrategyContext(
                market=market,
                positions={
                    market.ticker: Position(
                        ticker=market.ticker, position=position, avg_price=avg_price
                    )
                },
                balance=int(balance),
            )
            for order in strategy.on_market_data(ctx):
                if self.risk is not None and not self.risk.check(order, position).approved:
                    continue
                # Express every fill in YES terms: a NO buy is economically a
                # YES sell at (100 - no_price), which keeps PnL accounting unified.
                if order.side.value == "no":
                    yes_equiv_price = 100 - (order.no_price or order.yes_price or 0)
                    is_buy = order.action is Action.SELL
                else:
                    yes_equiv_price = order.yes_price or order.no_price or 0
                    is_buy = order.action is Action.BUY

                qty = order.count if is_buy else -order.count
                fee = self.fee_cents_per_contract * order.count
                balance -= qty * yes_equiv_price + fee
                report.fees_paid_cents += fee

                position, avg_price, realized = _apply_fill(
                    position, avg_price, qty, yes_equiv_price
                )
                if realized is not None:
                    report.trade_pnls.append(realized - fee)

                report.fills.append(
                    Fill(
                        ticker=order.ticker,
                        side=order.side,
                        action=order.action,
                        count=order.count,
                        price=order.yes_price or order.no_price or 1,
                    )
                )
                report.trades += 1

            mark = market.last_price or market.yes_bid or 0
            report.equity_curve.append(int(balance + position * mark))
        strategy.on_stop()

        report.final_equity_cents = report.equity_curve[-1] if report.equity_curve else int(balance)
        report.final_position = position
        return report


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
