"""Offline prospective campaign registry. Does NOT launch any collection.

Immutable UTC boundaries prevent labels or data gaps from moving forecasts
between development, calibration-selection and untouched final-test cohorts.
SQLite is local staging, not a claim of cloud durability. Export with backup(),
never copy an active .db while ignoring its WAL.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

from kalshi_bot.btc_model import MODEL_ID, REFERENCE_SOURCE
from kalshi_bot.btc_rules import RULE_FAMILY
from kalshi_bot.safety import require_simulation


@dataclass(frozen=True)
class Protocol:
    registered_at: float
    starts_at: float
    development_ends_at: float
    test_starts_at: float
    deadline: float
    source_sha256: str
    ledger_sha256: str
    model_sha256: str
    model_id: str = MODEL_ID
    rule_family: str = RULE_FAMILY
    reference_source: str = REFERENCE_SOURCE
    development_minimum: int = 100
    selection_minimum: int = 100
    test_minimum: int = 200
    horizon_seconds: int = 300
    tolerance_seconds: int = 30
    maximum_campaign_seconds: int = 604800
    entries_enabled: bool = False
    paid_hosting_approved: bool = False
    collection_approved: bool = False

    def validate(self) -> None:
        dates = (
            self.registered_at,
            self.starts_at,
            self.development_ends_at,
            self.test_starts_at,
            self.deadline,
        )
        if (
            any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in dates)
            or not all(a < b for a, b in pairwise(dates))
            or self.deadline - self.starts_at > 604800
            or self.maximum_campaign_seconds != 604800
            or self.entries_enabled
            or self.paid_hosting_approved
            or self.collection_approved
            or (self.development_minimum, self.selection_minimum, self.test_minimum)
            != (100, 100, 200)
            or (self.horizon_seconds, self.tolerance_seconds) != (300, 30)
            or (self.model_id, self.rule_family, self.reference_source)
            != (MODEL_ID, RULE_FAMILY, REFERENCE_SOURCE)
        ):
            raise ValueError("Invalid observation-only prospective protocol.")
        for value in (self.source_sha256, self.ledger_sha256, self.model_sha256):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError("Frozen SHA-256 fingerprints required.")

    def canonical(self) -> str:
        self.validate()
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)

    def cohort(self, predicted_at: float) -> str:
        if not math.isfinite(predicted_at) or not self.starts_at <= predicted_at < self.deadline:
            raise ValueError("Forecast outside original campaign window.")
        if predicted_at < self.development_ends_at:
            return "development"
        if predicted_at < self.test_starts_at:
            return "selection"
        return "test"


class CampaignRegistry:
    """Unique records across segments; idempotent retries, conflicting retries rejected.

    Records must come from a separately verified collector/evaluator. This registry
    does NOT establish feed provenance or make a self-declared protocol approved.
    """

    def __init__(self, path: Path, protocol: Protocol) -> None:
        require_simulation()
        canonical = protocol.canonical()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.protocol = protocol
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS campaign_manifest(
                id INTEGER PRIMARY KEY CHECK(id=1), canonical TEXT NOT NULL, sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS campaign_forecasts(
                ticker TEXT PRIMARY KEY, predicted_at REAL NOT NULL,
                closes_at REAL NOT NULL, probability REAL NOT NULL,
                cohort TEXT NOT NULL, evidence_sha256 TEXT NOT NULL,
                outcome INTEGER, confirmed_at REAL, outcome_evidence_sha256 TEXT
            );
        """)
        try:
            with self.db:
                self.db.execute(
                    "INSERT OR IGNORE INTO campaign_manifest VALUES(1,?,?)",
                    (canonical, hashlib.sha256(canonical.encode()).hexdigest()),
                )
                row = self.db.execute(
                    "SELECT canonical FROM campaign_manifest WHERE id=1"
                ).fetchone()
                if row is None or row[0] != canonical:
                    raise ValueError(
                        "Campaign protocol changed; refusing resume/deadline extension."
                    )
        except BaseException:
            self.db.close()
            raise

    def close(self) -> None:
        self.db.close()

    def forecast(
        self,
        ticker: str,
        predicted_at: float,
        closes_at: float,
        probability: float,
        evidence_sha256: str,
    ) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._forecast_in_transaction(
                ticker, predicted_at, closes_at, probability, evidence_sha256
            )

    def _forecast_in_transaction(
        self,
        ticker: str,
        predicted_at: float,
        closes_at: float,
        probability: float,
        evidence_sha256: str,
    ) -> None:
        """Shared atomic writer for the offline supervisor; never an approval."""
        if not self.db.in_transaction:
            raise ValueError("Forecast requires an enclosing transaction.")
        cohort = self.protocol.cohort(predicted_at)
        if (
            not ticker.startswith("KXBTC15M-")
            or isinstance(probability, bool)
            or not math.isfinite(probability)
            or not 0 <= probability <= 1
            or not math.isfinite(closes_at)
            or not 270 <= closes_at - predicted_at <= 330
            or len(evidence_sha256) != 64
            or any(c not in "0123456789abcdef" for c in evidence_sha256)
        ):
            raise ValueError("Invalid fixed-horizon forecast evidence.")
        fields = (ticker, predicted_at, closes_at, probability, cohort, evidence_sha256)
        old = self.db.execute(
            "SELECT ticker,predicted_at,closes_at,probability,cohort,evidence_sha256 "
            "FROM campaign_forecasts WHERE ticker=?",
            (ticker,),
        ).fetchone()
        if old is not None and tuple(old) != fields:
            raise ValueError("Conflicting same-market forecast; never overwrite cohort.")
        self.db.execute(
            "INSERT OR IGNORE INTO campaign_forecasts VALUES(?,?,?,?,?,?,NULL,NULL,NULL)", fields
        )

    def outcome(
        self,
        ticker: str,
        outcome: int,
        confirmed_at: float,
        evidence_sha256: str,
    ) -> None:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            self._outcome_in_transaction(ticker, outcome, confirmed_at, evidence_sha256)

    def _outcome_in_transaction(
        self,
        ticker: str,
        outcome: int,
        confirmed_at: float,
        evidence_sha256: str,
    ) -> None:
        if not self.db.in_transaction:
            raise ValueError("Outcome requires an enclosing transaction.")
        row = self.db.execute(
            "SELECT * FROM campaign_forecasts WHERE ticker=?", (ticker,)
        ).fetchone()
        if (
            row is None
            or type(outcome) is not int
            or outcome not in (0, 1)
            or not math.isfinite(confirmed_at)
            or not row["closes_at"] <= confirmed_at <= self.protocol.deadline
            or len(evidence_sha256) != 64
            or any(c not in "0123456789abcdef" for c in evidence_sha256)
        ):
            raise ValueError("Unverified, late or missing forecast/outcome evidence.")
        if row["outcome"] is not None and (
            row["outcome"],
            row["confirmed_at"],
            row["outcome_evidence_sha256"],
        ) != (outcome, confirmed_at, evidence_sha256):
            raise ValueError("Conflicting finalized outcome; manual review required.")
        self.db.execute(
            "UPDATE campaign_forecasts SET outcome=?,confirmed_at=?,outcome_evidence_sha256=? "
            "WHERE ticker=?",
            (outcome, confirmed_at, evidence_sha256, ticker),
        )

    def readiness(self) -> dict[str, Any]:
        counts = {
            cohort: self.db.execute(
                "SELECT COUNT(*) FROM campaign_forecasts WHERE cohort=? AND outcome IS NOT NULL",
                (cohort,),
            ).fetchone()[0]
            for cohort in ("development", "selection", "test")
        }
        late = self.db.execute(
            "SELECT COUNT(*) FROM campaign_forecasts WHERE cohort!='test' "
            "AND (confirmed_at IS NULL OR confirmed_at>=?)",
            (self.protocol.test_starts_at,),
        ).fetchone()[0]
        return {
            "finalized_distinct_markets": counts,
            "minimum_sample_counts_met": (
                counts["development"] >= 100
                and counts["selection"] >= 100
                and counts["test"] >= 200
            ),
            "pretest_label_leakage_or_pending": late,
            "protocol_authorization_verified": False,
            "data_provenance_verified": False,
            "model_performance_validated": False,
            "entries_enabled": False,
            "collection_started": False,
        }

    def backup(self, destination: Path) -> None:
        if (
            destination.exists()
            or destination.resolve()
            == Path(self.db.execute("PRAGMA database_list").fetchone()[2]).resolve()
        ):
            raise ValueError("Backup requires a new destination.")
        with closing(sqlite3.connect(destination)) as backup:
            self.db.backup(backup)
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity failure.")
