"""Strictly recognize the verified demo BTC 15-minute contract family.

No ticker-name heuristic is sufficient to establish settlement semantics.
Unknown rule wording, source, rounding or timing is deliberately unsupported.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

SERIES = "KXBTC15M"
RULE_FAMILY = "CF_BRTI_60S_GE_2DP_V1"
TERMS_URL = "https://assets.kalshi.com/contract_terms/CRYPTO15M.pdf"
SECONDARY = (
    "Not all cryptocurrency price data is the same. While checking a source like Google or "
    "Coinbase may help guide your decision, the price used to determine this market is based "
    "on CF Benchmarks' corresponding Real Time Index (RTI). At the last minute before "
    "expiration, 60 RTI prices are collected. The official and final value is the average "
    "of these prices, rounded to the nearest 2 decimal places."
)


def utc_timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timezone-aware timestamp required.")
    return parsed.timestamp()


def _rule_time(value: str) -> float:
    found = re.fullmatch(r"(\d{1,2}:\d{2} [AP]M) (EST|EDT) on ([A-Za-z]+ \d{1,2}, \d{4})", value)
    if not found:
        raise ValueError("Unsupported rule date.")
    clock, zone, date = found.groups()
    parsed = None
    for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            parsed = datetime.strptime(date + " " + clock, fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        raise ValueError("Unsupported rule date.")
    for fold in (0, 1):
        localized = parsed.replace(tzinfo=ZoneInfo("America/New_York"), fold=fold)
        if localized.tzname() == zone:
            return localized.timestamp()
    raise ValueError("Rule timezone disagrees with DST.")


@dataclass(frozen=True)
class VerifiedMarket:
    ticker: str
    opens_at: float
    closes_at: float
    administrative_expiration: float
    strike: float
    rules_sha256: str
    rule_family: str = RULE_FAMILY


def verify_series(series: dict[str, Any]) -> None:
    if (
        series.get("ticker") != SERIES
        or series.get("frequency") != "fifteen_min"
        or series.get("contract_terms_url") != TERMS_URL
        or series.get("fee_type") != "quadratic"
        or series.get("fee_multiplier") != 1
        or series.get("settlement_sources")
        != [{"name": "CF Benchmarks", "url": "https://www.cfbenchmarks.com/"}]
    ):
        raise ValueError("Unsupported series rules, settlement source or fee metadata.")


def verify_market(
    raw: dict[str, Any],
    series: dict[str, Any],
    *,
    now: float,
    finalized: bool = False,
) -> VerifiedMarket:
    verify_series(series)
    primary = raw.get("rules_primary", "")
    match = re.fullmatch(
        r"If the simple average of the sixty seconds of CF Benchmarks' BRTI before (.+) "
        r"is at least the simple average of the sixty seconds of CF Benchmarks' BRTI "
        r"before (.+), then the market resolves to Yes\.",
        primary,
    )
    if not match or raw.get("rules_secondary") != SECONDARY:
        raise ValueError("Unknown settlement condition or rounding rule.")
    close_rule, open_rule = map(_rule_time, match.groups())
    opens = utc_timestamp(raw["open_time"])
    closes = utc_timestamp(raw["close_time"])
    expiration = utc_timestamp(raw["expiration_time"])
    try:
        strike_decimal = Decimal(str(raw["floor_strike"]))
    except InvalidOperation as exc:
        raise ValueError("Invalid market strike.") from exc
    ticker = str(raw["ticker"])
    event = str(raw["event_ticker"])
    if (
        raw.get("status") != ("finalized" if finalized else "active")
        or raw.get("market_type") != "binary"
        or raw.get("strike_type") != "greater_or_equal"
        or raw.get("custom_strike", {}).get("round_digits") != "2"
        or (
            raw.get("result") not in ("yes", "no")
            if finalized
            else raw.get("result") not in ("", None)
        )
        or not event.startswith(SERIES + "-")
        or not ticker.startswith(event + "-")
        or not strike_decimal.is_finite()
        or not Decimal("0") < strike_decimal <= Decimal("100000000")
        or strike_decimal != strike_decimal.quantize(Decimal("0.01"))
        or raw.get("notional_value_dollars") != "1.0000"
        or closes - opens != 900
        or opens % 900 != 0
        or (open_rule, close_rule) != (opens, closes)
        or (not closes <= now if finalized else not opens <= now < closes)
        or not closes <= expiration <= closes + 7 * 86400
    ):
        raise ValueError("Unverified market metadata, strike, status or expiry.")
    digest = hashlib.sha256((primary + "\n" + SECONDARY).encode()).hexdigest()
    return VerifiedMarket(ticker, opens, closes, expiration, float(strike_decimal), digest)
