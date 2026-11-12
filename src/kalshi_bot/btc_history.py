"""Bounded public demo historical-data audit, never a fabricated fill backtest."""

from __future__ import annotations

import time
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from urllib.parse import quote

import httpx

from kalshi_bot.btc_data import DemoBTCSource, validate_response
from kalshi_bot.btc_rules import SERIES, utc_timestamp, verify_market, verify_series
from kalshi_bot.safety import require_simulation

FEE_SOURCE = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"


async def history_audit(source: DemoBTCSource, limit: int = 200) -> dict[str, Any]:
    """Audit a bounded sample, including both live and historical partitions.

    Finalized metadata is label evidence, not evidence of pre-close availability.
    Candles are never converted into executable quotes or interpolated ticks.
    """
    require_simulation()
    if not 1 <= limit <= 200:
        raise ValueError("Audit market limit must be between 1 and 200.")
    now = time.time()
    series_body, _ = await source.get("/series/" + SERIES)
    series = series_body["series"]
    verify_series(series)
    cutoff, _ = await source.get("/historical/cutoff")
    found: dict[str, tuple[dict[str, Any], bool]] = {}
    partitions = []
    retrieved = 0
    for archived in (False, True):
        path = "/historical/markets" if archived else "/markets"
        cursor = ""
        seen: set[str] = set()
        for page in range(3):
            page_limit = limit if len(found) < limit else 1
            params: dict[str, Any] = {
                "series_ticker": SERIES,
                "limit": page_limit,
                "cursor": cursor,
            }
            if not archived:
                params["status"] = "settled"
            body, _ = await source.get(path, params)
            retrieved += len(body["markets"])
            for raw in body["markets"]:
                if len(found) < limit and raw["ticker"] not in found:
                    found[raw["ticker"]] = (raw, archived)
            cursor = body["cursor"]
            if len(found) >= limit or not cursor:
                partitions.append(
                    {
                        "archived": archived,
                        "pages": page + 1,
                        "has_more": bool(cursor),
                        "sample_truncated": bool(cursor),
                    }
                )
                break
            if not isinstance(cursor, str) or cursor in seen:
                raise ValueError("Historical audit cursor repeated.")
            seen.add(cursor)
        else:
            raise ValueError("Historical audit exceeded page budget.")
        # Always probe the historical partition, but do not inflate the chosen
        # recent sample beyond the caller's cap with older archived labels.
    now = time.time()
    good = []
    rejected = []
    for raw, archived in found.values():
        try:
            market = verify_market(raw, series, now=now, finalized=True)
            settled = utc_timestamp(raw["settlement_ts"])
            expected = Decimal(1) if raw["result"] == "yes" else Decimal(0)
            if (
                not market.closes_at <= settled <= now
                or Decimal(raw["settlement_value_dollars"]) != expected
            ):
                raise ValueError("Settlement timestamp or payout disagrees with result.")
            good.append((raw, market, archived))
        except (ValueError, TypeError, KeyError) as exc:
            rejected.append({"ticker": raw["ticker"], "reason": str(exc)})
    probes = []
    for _raw, market, archived in sorted(good, key=lambda x: x[1].closes_at, reverse=True)[:3]:
        path = (
            "/historical/markets/" + quote(market.ticker, safe="") + "/candlesticks"
            if archived
            else "/series/" + SERIES + "/markets/" + quote(market.ticker, safe="") + "/candlesticks"
        )
        body, _ = await source.get(
            path,
            {
                "start_ts": int(market.opens_at),
                "end_ts": int(market.closes_at),
                "period_interval": 1,
            },
        )
        if body["ticker"] != market.ticker:
            raise ValueError("Historical candle ticker mismatch.")
        candles = body["candlesticks"]
        if any(not market.opens_at < c["end_period_ts"] <= market.closes_at for c in candles):
            raise ValueError("Historical candle outside requested interval.")
        probes.append(
            {
                "ticker": market.ticker,
                "opens_at": market.opens_at,
                "closes_at": market.closes_at,
                "candles": candles,
                "resting_depth_provided": False,
            }
        )
    coinbase_candles: list[Any] = []
    reference_url = None
    if probes:
        start = min(p["opens_at"] for p in probes) - 180
        end = max(p["closes_at"] for p in probes)
        if end - start <= 299 * 60:
            params = {
                "granularity": 60,
                "start": datetime.fromtimestamp(start, UTC).isoformat(),
                "end": datetime.fromtimestamp(end, UTC).isoformat(),
            }
            async with httpx.AsyncClient(
                timeout=2,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                started = time.time()
                response = await client.get(
                    "https://api.exchange.coinbase.com/products/BTC-USD/candles",
                    params=params,
                    headers={"Cache-Control": "no-cache"},
                )
                validate_response(response, started)
                body = response.json()
                if not isinstance(body, list):
                    raise ValueError("Unexpected historical Coinbase response.")
                coinbase_candles = [
                    c for c in body if len(c) == 6 and start <= c[0] and c[0] + 60 <= end
                ]
                reference_url = str(response.request.url)
    return {
        "status": "blocked_insufficient_executable_history",
        "environment": "demo",
        "dry_run": True,
        "audited_at": now,
        "sample_limit": limit,
        "sample_scope": "bounded recent-first metadata sample",
        "partitions": partitions,
        "historical_cutoffs": cutoff,
        "retrieved_markets": retrieved,
        "sampled_markets": len(found),
        "verified_finalized_labels": len(good),
        "label_counts": dict(Counter(raw["result"] for raw, _, _ in good)),
        "rejected_markets": rejected,
        "candlestick_probes": probes,
        "coinbase_candle_count": len(coinbase_candles),
        "coinbase_candle_source": reference_url,
        "coinbase_candles": coinbase_candles,
        "raw_finalized_markets": [raw for raw, _, _ in good],
        "execution_eligible_market_sample": 0,
        "calibration_sample": 0,
        "historical_trade_count": 0,
        "historical_win_rate": None,
        "historical_pnl_cents": None,
        "historical_maximum_drawdown": None,
        "historical_brier": None,
        "historical_calibration_ece": None,
        "blockers": [
            "Public candle schema has no timestamped resting order-book depth.",
            "Minute OHLC is not a <=5-second reference history or CF BRTI average.",
            "Finalized metadata does not establish what was known before close.",
            "No audited out-of-time calibration artifact or eligible snapshot archive supplied.",
        ],
        "fee_evidence": {
            "source": FEE_SOURCE,
            "effective_date": "2026-07-07",
            "general_taker_formula": "M * 0.07 * C * P * (1-P)",
            "series_multiplier": series["fee_multiplier"],
            "simulated_reserve_cents_per_contract": 2,
            "scope": "current M=1 formula upper bound; historical schedule not reconstructed",
        },
        "bankroll_modified": False,
        "entries_enabled": False,
        "promotion_allowed": False,
    }
