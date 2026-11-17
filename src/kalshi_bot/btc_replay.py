"""Offline as-observed replay; candles/volume cannot masquerade as executable books."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal, TypeAlias, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kalshi_bot.btc_data import CoinbaseReference, DemoBTCSource
from kalshi_bot.btc_model import CalibrationArtifact, ReferenceTick
from kalshi_bot.btc_rules import VerifiedMarket, utc_timestamp, verify_market
from kalshi_bot.btc_strategy import BTCPaperRunner
from kalshi_bot.paper import PaperBroker, PaperQuote
from kalshi_bot.safety import require_simulation


class Observation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    provenance: Literal["observed-demo"]
    observed_at: float = Field(gt=0)
    series: dict[str, Any]
    market: dict[str, Any]


class ReplayFrame(Observation):
    kind: Literal["frame"]
    metadata_received_at: float = Field(gt=0)
    series_received_at: float = Field(gt=0)
    reference_received_at: float = Field(gt=0)
    reference: ReferenceTick
    quote: PaperQuote

    @model_validator(mode="after")
    def _as_of(self) -> ReplayFrame:
        market = verify_market(self.market, self.series, now=self.observed_at)
        received = (
            self.metadata_received_at,
            self.series_received_at,
            self.reference_received_at,
            self.quote.observed_at,
        )
        if (
            any(not 0 <= self.observed_at - t <= 5 for t in received)
            or not 0 <= self.reference_received_at - self.reference.timestamp <= 5
            or self.quote.ticker != market.ticker
        ):
            raise ValueError("Future/stale observation or mismatched snapshot ticker.")
        return self


class ReplayOutcome(Observation):
    kind: Literal["outcome"]

    @model_validator(mode="after")
    def _confirmed(self) -> ReplayOutcome:
        market = verify_market(self.market, self.series, now=self.observed_at, finalized=True)
        if not market.closes_at <= utc_timestamp(self.market["settlement_ts"]) <= self.observed_at:
            raise ValueError("Outcome was unavailable at its claimed observation time.")
        payout = "1.0000" if self.market["result"] == "yes" else "0.0000"
        if self.market.get("settlement_value_dollars") != payout:
            raise ValueError("Confirmed binary payout disagrees with label.")
        return self


class ReplayReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    kind: Literal["reference"]
    provenance: Literal["observed-demo"]
    observed_at: float = Field(gt=0)
    reference: ReferenceTick

    @model_validator(mode="after")
    def _fresh(self) -> ReplayReference:
        if not 0 <= self.observed_at - self.reference.timestamp <= 5:
            raise ValueError("Reference observation is stale or in the future.")
        return self


class ReplaySkip(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    kind: Literal["skip"]
    provenance: Literal["observed-demo"]
    observed_at: float = Field(gt=0)
    ticker: str | None = None
    reason: str = Field(min_length=1, max_length=512)


ReplayEvent: TypeAlias = ReplayFrame | ReplayOutcome | ReplayReference | ReplaySkip


def parse_event(raw: dict[str, Any]) -> ReplayEvent:
    if raw.get("kind") == "frame":
        return ReplayFrame.model_validate(raw)
    if raw.get("kind") == "outcome":
        return ReplayOutcome.model_validate(raw)
    if raw.get("kind") == "reference":
        return ReplayReference.model_validate(raw)
    if raw.get("kind") == "skip":
        return ReplaySkip.model_validate(raw)
    raise ValueError("Unsupported observation kind; candles are not executable snapshots.")


def append_observation(path: Path, event: dict[str, Any]) -> None:
    """Record normalized public observations only, never headers or credentials."""
    if path.suffix != ".jsonl":
        raise ValueError("Observation recording requires a .jsonl data file.")
    data = {**event, "provenance": "observed-demo"}
    parsed = parse_event(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(parsed.model_dump_json() + "\n")


def load_observations(path: Path) -> tuple[list[ReplayEvent], str]:
    if path.stat().st_size > 64_000_000:
        raise ValueError("Replay input exceeds 64 MB; use a bounded observation archive.")
    digest = hashlib.sha256()
    observations: list[ReplayEvent] = []
    outcomes: dict[str, str] = {}
    with path.open("rb") as stream:
        for line in stream:
            if len(line) > 200_000 or len(observations) >= 20000:
                raise ValueError("Replay line/event limit exceeded.")
            digest.update(line)
            raw = json.loads(line)
            event = parse_event(raw)
            if observations and event.observed_at < observations[-1].observed_at:
                raise ValueError("Archive is not in observation order; do not silently sort it.")
            if isinstance(event, ReplayOutcome):
                ticker, result = event.market["ticker"], event.market["result"]
                if ticker in outcomes and outcomes[ticker] != result:
                    raise ValueError("Conflicting outcomes require manual review.")
                outcomes[ticker] = result
            observations.append(event)
    if not any(isinstance(event, ReplayFrame) for event in observations):
        raise ValueError("No executable as-observed frames; candles/labels alone are insufficient.")
    return observations, digest.hexdigest()


class ReplaySource:
    def __init__(self, observations: list[ReplayEvent]) -> None:
        self.frame: ReplayFrame | None = None
        self.now = 0.0
        self.outcomes = [x for x in observations if isinstance(x, ReplayOutcome)]

    async def discover(self) -> tuple[list[VerifiedMarket], list[dict[str, str]]]:
        assert self.frame is not None
        frame = self.frame
        return [verify_market(frame.market, frame.series, now=self.now)], []

    async def refresh(self, market: VerifiedMarket) -> tuple[VerifiedMarket, PaperQuote]:
        assert self.frame is not None
        verified = verify_market(self.frame.market, self.frame.series, now=self.now)
        if verified != market:
            raise ValueError("Replay metadata changed.")
        return verified, self.frame.quote

    async def tick(self) -> ReferenceTick:
        assert self.frame is not None
        return self.frame.reference

    async def result(self, market: VerifiedMarket) -> tuple[int, str] | None:
        available = [
            x
            for x in self.outcomes
            if x.market["ticker"] == market.ticker and x.observed_at <= self.now
        ]
        if not available:
            return None
        event = available[-1]
        verified = verify_market(event.market, event.series, now=self.now, finalized=True)
        if verified != market:
            raise ValueError("Outcome rules differ from the pre-close observation.")
        return (
            100 if event.market["result"] == "yes" else 0,
            "https://demo-api.kalshi.co/trade-api/v2/markets/" + market.ticker,
        )


async def replay(
    path: Path,
    calibration: CalibrationArtifact | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """No HTTP clients, production paths, ledger resets or uncalibrated bypass."""
    require_simulation()
    observations, digest = load_observations(path)
    source = ReplaySource(observations)
    broker = PaperBroker(":memory:")
    try:
        # Structural adapter implements precisely the three asynchronous reads used
        # by the runner. No network client is created or inherited by replay.
        runner = BTCPaperRunner(
            broker,
            cast(DemoBTCSource, source),
            cast(CoinbaseReference, source),
            calibration,
            clock=lambda: source.now,
        )
        for event in observations:
            source.now = event.observed_at
            if isinstance(event, ReplayFrame):
                source.frame = event
                await runner.step()
            elif isinstance(event, ReplayOutcome):
                await runner._settle_known(source.now)
                with broker._transaction():
                    broker.db.execute(
                        "INSERT INTO btc_equity VALUES(?,?)",
                        (source.now, broker.equity({})),
                    )
            elif isinstance(event, ReplayReference):
                runner._remember(event.reference)
            else:
                if event.ticker:
                    broker.cancel_market(event.ticker)
                else:
                    runner._cancel_all()
                broker.record_event("recorded_skip", event.model_dump())
        runner._cancel_all()
        report = runner.report()
        events = broker.rows("events")
        report.update(
            {
                "status": "offline_policy_replay",
                "dataset_sha256": digest,
                "observation_events": len(observations),
                "source_market_sample": len(
                    {x.market["ticker"] for x in observations if isinstance(x, Observation)}
                ),
                "snapshot_market_sample": len(
                    {x.market["ticker"] for x in observations if isinstance(x, ReplayFrame)}
                ),
                "provenance": "declared observed-demo; independent integrity review still required",
                "audit_event_counts": dict(Counter(x["kind"] for x in events)),
                "fill_events": len(broker.rows("fills")),
                "minimum_fill_latency_seconds": 1,
                "participation_fraction": 0.25,
                "persistent_bankroll_modified": False,
                "calibration_auto_exported": False,
                "promotion_allowed": False,
                "profitability_established": False,
            }
        )
        return report, events
    finally:
        broker.close()
