"""Typed configuration loaded from environment variables and ``.env``.

All settings are validated at startup via pydantic. Secrets (API key ID and the
path to the RSA private key) come from the environment — never hard-code them.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(str, Enum):
    """Kalshi trading environments."""

    PROD = "prod"
    DEMO = "demo"


# Base URLs per environment. The official starter code uses the
# ``api.elections.kalshi.com`` / ``demo-api.kalshi.co`` hosts; these are exposed
# as config so you can switch to ``external-api.*`` if Kalshi migrates.
REST_BASE_URLS: dict[Environment, str] = {
    Environment.PROD: "https://api.elections.kalshi.com/trade-api/v2",
    Environment.DEMO: "https://demo-api.kalshi.co/trade-api/v2",
}

WS_BASE_URLS: dict[Environment, str] = {
    Environment.PROD: "wss://api.elections.kalshi.com/trade-api/ws/v2",
    Environment.DEMO: "wss://demo-api.kalshi.co/trade-api/ws/v2",
}


class Settings(BaseSettings):
    """Runtime configuration for the bot.

    Values are read from environment variables prefixed with ``KALSHI_`` and
    from a ``.env`` file in the working directory.
    """

    model_config = SettingsConfigDict(
        env_prefix="KALSHI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Credentials -----------------------------------------------------
    api_key_id: str = Field(default="", description="Kalshi API key ID (UUID).")
    private_key_path: str = Field(
        default="./secrets/kalshi_private_key.pem",
        description="Path to the RSA private key PEM file.",
    )

    # --- Environment -----------------------------------------------------
    environment: Environment = Field(
        default=Environment.DEMO,
        description="Trading environment: 'demo' (sandbox) or 'prod'.",
    )

    # --- Safety ----------------------------------------------------------
    dry_run: bool = Field(
        default=True,
        description="If true, simulate orders instead of sending them to Kalshi.",
    )

    # --- HTTP ------------------------------------------------------------
    request_timeout: float = Field(default=10.0, description="HTTP timeout in seconds.")

    @property
    def rest_base_url(self) -> str:
        """REST base URL for the configured environment."""
        return REST_BASE_URLS[self.environment]

    @property
    def ws_base_url(self) -> str:
        """WebSocket base URL for the configured environment."""
        return WS_BASE_URLS[self.environment]


def load_settings() -> Settings:
    """Load and validate settings from the environment."""
    return Settings()
