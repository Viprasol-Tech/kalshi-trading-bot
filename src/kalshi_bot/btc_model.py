"""Experimental Coinbase-proxy probability estimator and out-of-time gate.

This model is NOT a CF BRTI feed and is not validated by synthetic tests.
It approximates an arithmetic final-minute average using a driftless local
normal-price diffusion. Entry requires separate, matching empirical validation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise
from statistics import NormalDist
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kalshi_bot.btc_rules import RULE_FAMILY

MODEL_ID = "coinbase-proxy-normal-final-minute-v1"
REFERENCE_SOURCE = "coinbase:BTC-USD"


class ReferenceTick(BaseModel):
    model_config = ConfigDict(frozen=True, allow_inf_nan=False)
    source: str = REFERENCE_SOURCE
    timestamp: float = Field(gt=0)
    price: float = Field(gt=0)


@dataclass(frozen=True)
class Estimate:
    yes_probability: float
    volatility_per_sqrt_second: float
    reference_samples: int
    seconds_to_close: float
    model_id: str = MODEL_ID
    source: str = REFERENCE_SOURCE


def estimate(
    ticks: list[ReferenceTick],
    strike: float,
    closes_at: float,
    *,
    now: float,
) -> Estimate:
    seconds = closes_at - now
    if (
        not all(math.isfinite(x) for x in (strike, closes_at, now))
        or strike <= 0
        or not 70 <= seconds <= 840
    ):
        raise ValueError(
            "Unsupported horizon; final-minute averaging already underway or too early."
        )
    if len(ticks) < 120 or len(ticks) > 1800:
        raise ValueError("At least 120 recent reference observations required.")
    if any(t.source != REFERENCE_SOURCE for t in ticks):
        raise ValueError("Reference source mismatch.")
    if not 0 <= now - ticks[-1].timestamp <= 5:
        raise ValueError("Reference price is stale or in the future.")
    if ticks[0].timestamp < now - 1800 or ticks[-1].timestamp - ticks[0].timestamp < 120:
        raise ValueError("Insufficient recent time coverage.")
    squared, elapsed = 0.0, 0.0
    for before, after in pairwise(ticks):
        dt = after.timestamp - before.timestamp
        if not 0 < dt <= 5:
            raise ValueError("Reference observations are duplicated, out of order or gapped.")
        value = math.log(after.price / before.price)
        if abs(value) > 0.02:
            raise ValueError("Reference jump is outside model assumptions.")
        squared += value * value
        elapsed += dt
    sigma = math.sqrt(squared / elapsed)
    if not 0.000001 <= sigma <= 0.005:
        raise ValueError("Degenerate or unsupported volatility.")
    # For Brownian price increments, Var(mean over [T-L,T]) = sigma²*(T-2L/3).
    # This is an approximation for the arithmetic index average, not terminal spot.
    effective_seconds = seconds - 40.0
    spot = ticks[-1].price
    sd = spot * sigma * math.sqrt(effective_seconds)
    # YES includes equality after two-decimal rounding. Use the lower rounding boundary.
    probability = NormalDist().cdf((spot - (strike - 0.005)) / sd)
    return Estimate(min(0.999, max(0.001, probability)), sigma, len(ticks), seconds)


class CalibrationRecord(BaseModel):
    """One pre-close forecast per distinct market and its later confirmed result."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    ticker: str = Field(min_length=1)
    rules_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    strike: float = Field(gt=0)
    predicted_at: float = Field(gt=0)
    closes_at: float = Field(gt=0)
    resolved_at: float = Field(gt=0)
    yes_outcome: int = Field(ge=0, le=1, strict=True)
    label_source: str
    ticks: list[ReferenceTick]


class CalibrationArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    model_id: str = MODEL_ID
    rule_family: str = RULE_FAMILY
    source: str = REFERENCE_SOURCE
    provenance: str
    fitted_before: float = Field(gt=0)
    evaluated_at: float = Field(gt=0)
    records: list[CalibrationRecord]

    @model_validator(mode="after")
    def _metadata(self) -> CalibrationArtifact:
        if (
            self.model_id != MODEL_ID
            or self.rule_family != RULE_FAMILY
            or self.source != REFERENCE_SOURCE
            or self.provenance != "observed-demo"
        ):
            raise ValueError("Calibration must match this model/rule/feed and observed demo data.")
        return self


def calibration_check(
    artifact: CalibrationArtifact | None,
    *,
    now: float,
    horizon: float,
    yes_probability: float,
) -> dict[str, Any]:
    """Recompute held-out scores; never trust precomputed metrics in a file.

    This establishes a validation gate, not statistical proof of profitability.
    Label evidence and collection integrity still require Phase 4 review.
    """
    if artifact is None:
        return {"passed": False, "reason": "no observed out-of-time calibration", "sample_size": 0}
    if not (
        math.isfinite(now)
        and math.isfinite(horizon)
        and math.isfinite(yes_probability)
        and 70 <= horizon <= 840
        and 0 <= yes_probability <= 1
    ):
        return {"passed": False, "reason": "invalid current calibration query"}
    if (
        artifact.evaluated_at > now
        or now - artifact.evaluated_at > 7 * 86400
        or len(artifact.records) < 200
        or len(artifact.records) > 20000
        or not artifact.fitted_before < artifact.evaluated_at
    ):
        return {"passed": False, "reason": "stale or inadequate calibration sample"}
    probabilities, labels, seen = [], [], set()
    relevant: list[tuple[float, int]] = []
    for record in artifact.records:
        if (
            record.ticker in seen
            or not record.ticker.startswith("KXBTC15M-")
            or not artifact.fitted_before < record.predicted_at < record.closes_at
            or not record.closes_at <= record.resolved_at <= artifact.evaluated_at
            or record.label_source
            != ("https://demo-api.kalshi.co/trade-api/v2/markets/" + record.ticker)
        ):
            return {"passed": False, "reason": "duplicate, leaked or unverified calibration record"}
        seen.add(record.ticker)
        try:
            p = estimate(
                record.ticks, record.strike, record.closes_at, now=record.predicted_at
            ).yes_probability
        except ValueError:
            return {"passed": False, "reason": "invalid calibration feature history"}
        probabilities.append(p)
        labels.append(record.yes_outcome)
        if int((record.closes_at - record.predicted_at) // 180) == int(horizon // 180) and min(
            4, int(p * 5)
        ) == min(4, int(yes_probability * 5)):
            relevant.append((p, record.yes_outcome))
    pairs = list(zip(probabilities, labels, strict=True))
    brier = sum((p - y) ** 2 for p, y in pairs) / len(pairs)
    prevalence = sum(labels) / len(labels)
    baseline = sum((prevalence - y) ** 2 for y in labels) / len(labels)
    ece = 0.0
    max_error = 0.0
    for index in range(5):
        group = [(p, y) for p, y in pairs if min(4, int(p * 5)) == index]
        if group:
            error = abs(sum(p - y for p, y in group) / len(group))
            ece += len(group) / len(pairs) * error
            max_error = max(max_error, error)
    local_error = abs(sum(p - y for p, y in relevant) / len(relevant)) if relevant else 1.0
    passed = (
        brier < baseline
        and ece <= 0.05
        and max_error <= 0.10
        and local_error <= 0.10
        and len(relevant) >= 30
    )
    return {
        "passed": passed,
        "reason": "gate passed" if passed else "calibration thresholds not met",
        "sample_size": len(pairs),
        "horizon_sample_size": len(relevant),
        "brier": brier,
        "baseline_brier": baseline,
        "ece": ece,
        "max_bin_error": max_error,
        "local_calibration_error": local_error,
        "probability_haircut": max(0.05, max_error, local_error),
    }
