"""Offline-only projection of decoded demo WebSocket messages.

No sockets, keys, clocks, persistence, automatic reconnect or engine dispatch.
Snapshot send/receive times do not establish quote-event time. A delta only
dates its changed level; it cannot refresh any untouched price or the whole book.
"""

from __future__ import annotations

import math
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from kalshi_bot.demo_quote_ws import DemoBookSession, QuoteEvent, ResnapshotRequired
from kalshi_bot.safety import SimulationOnlyError, require_simulation


class OfflineQuoteAdapter:
    """One connection epoch. Failed epochs cannot be reset or merged on resume."""

    def __init__(self, tickers: list[str]) -> None:
        require_simulation()
        self.session = DemoBookSession(tickers)
        self.events: dict[str, QuoteEvent] = {}
        self.level_times: dict[str, dict[str, dict[Decimal, float | None]]] = {}

    def consume(
        self, raw: dict[str, Any], *, received_at: float, monotonic: float
    ) -> dict[str, Any]:
        require_simulation()
        try:
            event = self.session.consume(raw, received_at=received_at, monotonic=monotonic)
            ticker = event.ticker
            if event.kind == "orderbook_snapshot":
                self.level_times[ticker] = {
                    side: dict.fromkeys(self.session.books[ticker][side]) for side in ("yes", "no")
                }
            else:
                msg = raw["msg"]
                side, price = msg["side"], Decimal(msg["price_dollars"])
                # A zero-delta heartbeat must not rejuvenate an old quote.
                if Decimal(msg["delta_fp"]) != 0:
                    if price in self.session.books[ticker][side]:
                        self.level_times[ticker][side][price] = event.event_at
                    else:
                        self.level_times[ticker][side].pop(price, None)
            self.events[ticker] = event
            return self.diagnose(ticker, now=received_at, monotonic=monotonic)
        except ResnapshotRequired:
            self.events.clear()
            self.level_times.clear()
            raise

    def diagnose(self, ticker: str, *, now: float, monotonic: float) -> dict[str, Any]:
        require_simulation()
        if (
            type(now) not in (int, float)
            or type(monotonic) not in (int, float)
            or not math.isfinite(now)
            or not math.isfinite(monotonic)
            or now <= 0
            or monotonic < 0
        ):
            raise ValueError("Explicit finite observation clocks required.")
        reasons = ["snapshot_event_time_unverified", "offline_diagnostic_not_execution_feed"]
        event = self.events.get(ticker)
        if self.session.failed or event is None:
            return {
                "ticker": ticker,
                "session_valid": not self.session.failed,
                "book_state": "unavailable",
                "whole_book_freshness_verified": False,
                "execution_eligible": False,
                "entries_enabled": False,
                "reasons": [*reasons, "fresh_snapshot_required"],
            }
        age, elapsed = now - event.received_at, monotonic - event.received_monotonic
        receive_fresh = 0 <= age <= 5 and 0 <= elapsed <= 5 and abs(age - elapsed) <= 2
        if not receive_fresh:
            reasons.append("stale_or_discontinuous_receive_clock")
        levels = self.session.books[ticker]
        sides: dict[str, dict[str, Any]] = {}
        for side in ("yes", "no"):
            price = max(levels[side], default=None)
            stamp = self.level_times[ticker][side].get(price) if price is not None else None
            sides[side] = {
                "bid": str(price) if price is not None else None,
                "depth": str(levels[side][price]) if price is not None else None,
                "level_event_at": stamp,
                "level_event_fresh": (
                    receive_fresh and stamp is not None and 0 <= now - stamp <= 5
                ),
                "timestamp_scope": "changed_price_level_only" if stamp is not None else "unknown",
            }
        y, n = sides["yes"]["bid"], sides["no"]["bid"]
        book_state = (
            "empty"
            if y is None and n is None
            else "yes_only"
            if n is None
            else "no_only"
            if y is None
            else "two_sided"
        )
        if book_state != "two_sided":
            reasons.append("incomplete_book")
        # Complementary asks are actual opposite-side resting bids only. No
        # missing bid, quantity or timestamp is ever filled in from another feed.
        for side, other in (("yes", "no"), ("no", "yes")):
            bid = sides[other]["bid"]
            sides[side]["ask"] = str(Decimal(1) - Decimal(bid)) if bid is not None else None
            sides[side]["ask_depth"] = sides[other]["depth"]
        return {
            "ticker": ticker,
            "session_valid": True,
            "book_state": book_state,
            "quote_sides": sides,
            "latest_message": asdict(event),
            "receive_fresh": receive_fresh,
            "delta_event_fresh": (
                receive_fresh and event.event_at is not None and 0 <= now - event.event_at <= 5
            ),
            "event_to_receipt_seconds": (
                event.received_at - event.event_at if event.event_at is not None else None
            ),
            "send_to_receipt_seconds": (
                event.received_at - event.sending_at if event.sending_at is not None else None
            ),
            "whole_book_freshness_verified": False,
            "execution_eligible": False,
            "entries_enabled": False,
            "reasons": reasons,
        }

    def as_execution_book(self) -> None:
        """A fresh message or fresh top levels are not an execution release."""
        raise SimulationOnlyError(
            "Offline adapter cannot release books: whole-book event-time provenance "
            "and separately reviewed data/model/paper gates remain required."
        )
