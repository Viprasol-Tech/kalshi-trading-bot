"""Tests for typed configuration and risk-settings wiring."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from kalshi_bot.config import Environment, RiskSettings, Settings
from kalshi_bot.risk.manager import RiskLimits, RiskManager


def test_defaults_are_demo_and_dry_run() -> None:
    s = Settings(_env_file=None)
    assert s.environment is Environment.DEMO
    assert s.dry_run is True
    assert s.rest_base_url.startswith("https://demo-api.kalshi.co")
    assert s.ws_base_url.startswith("wss://demo-api.kalshi.co")


def test_prod_urls() -> None:
    s = Settings(_env_file=None, environment=Environment.PROD)
    assert "api.elections.kalshi.com" in s.rest_base_url
    assert s.ws_base_url.startswith("wss://")


def test_api_key_is_stripped() -> None:
    s = Settings(_env_file=None, api_key_id="  abc-123  ")
    assert s.api_key_id == "abc-123"


def test_risk_settings_validation() -> None:
    with pytest.raises(ValidationError):
        RiskSettings(kelly_fraction=2.0)  # must be <= 1.0
    with pytest.raises(ValidationError):
        RiskSettings(max_contracts_per_order=0)  # must be > 0


def test_risk_manager_from_settings() -> None:
    rs = RiskSettings(max_contracts_per_order=7, kelly_fraction=0.5)
    rm = RiskManager.from_settings(rs)
    assert isinstance(rm.limits, RiskLimits)
    assert rm.limits.max_contracts_per_order == 7
    assert rm.limits.kelly_fraction == 0.5


def test_env_overrides_nested_risk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KALSHI_RISK__MAX_CONTRACTS_PER_ORDER", "33")
    s = Settings(_env_file=None)
    assert s.risk.max_contracts_per_order == 33
