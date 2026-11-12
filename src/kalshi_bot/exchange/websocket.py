"""Kalshi WebSocket client for real-time market and account data.

Auth uses the same RSA-PSS signing as REST, but the headers are sent on the
WebSocket **handshake** (signed with method ``GET`` and path
``/trade-api/ws/v2``). Channels include ``orderbook_delta``, ``ticker``,
``trade``, ``fill`` and ``market_positions``.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.config import Settings
from kalshi_bot.safety import SimulationOnlyError

WS_PATH = "/trade-api/ws/v2"


class KalshiWebSocket:
    """Minimal async Kalshi WebSocket client."""

    def __init__(
        self,
        ws_url: str,
        api_key_id: str,
        private_key: rsa.RSAPrivateKey,
    ) -> None:
        raise SimulationOnlyError("Authenticated exchange WebSockets are disabled.")

    @classmethod
    def from_settings(cls, settings: Settings) -> KalshiWebSocket:
        """Build a WebSocket client from settings (requires credentials)."""
        raise SimulationOnlyError("Authenticated exchange WebSockets are disabled.")

    def _handshake_headers(self) -> dict[str, str]:
        raise SimulationOnlyError("Authenticated exchange WebSockets are disabled.")

    async def stream(
        self,
        channels: list[str],
        market_tickers: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Connect, subscribe, and yield each decoded message.

        Args:
            channels: Channel names, e.g. ``["orderbook_delta", "ticker"]``.
            market_tickers: Markets to subscribe to (omit for account channels).

        Yields:
            Each parsed JSON message from the server.
        """
        raise SimulationOnlyError("Authenticated exchange WebSockets are disabled.")
        yield {}  # Keep the async-iterator API; execution always fails closed above.
