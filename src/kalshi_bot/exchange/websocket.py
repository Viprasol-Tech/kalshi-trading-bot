"""Kalshi WebSocket client for real-time market and account data.

Auth uses the same RSA-PSS signing as REST, but the headers are sent on the
WebSocket **handshake** (signed with method ``GET`` and path
``/trade-api/ws/v2``). Channels include ``orderbook_delta``, ``ticker``,
``trade``, ``fill`` and ``market_positions``.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import websockets
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.config import Settings
from kalshi_bot.exchange.auth import build_auth_headers, load_private_key

WS_PATH = "/trade-api/ws/v2"


class KalshiWebSocket:
    """Minimal async Kalshi WebSocket client."""

    def __init__(
        self,
        ws_url: str,
        api_key_id: str,
        private_key: rsa.RSAPrivateKey,
    ) -> None:
        self._ws_url = ws_url
        self._api_key_id = api_key_id
        self._private_key = private_key
        self._id_counter = itertools.count(1)

    @classmethod
    def from_settings(cls, settings: Settings) -> KalshiWebSocket:
        """Build a WebSocket client from settings (requires credentials)."""
        if not settings.api_key_id:
            raise ValueError("WebSocket auth requires KALSHI_API_KEY_ID to be set.")
        private_key = load_private_key(settings.private_key_path)
        return cls(settings.ws_base_url, settings.api_key_id, private_key)

    def _handshake_headers(self) -> dict[str, str]:
        path = urlsplit(self._ws_url).path or WS_PATH
        timestamp_ms = int(time.time() * 1000)
        return build_auth_headers(self._api_key_id, self._private_key, timestamp_ms, "GET", path)

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
        async with websockets.connect(
            self._ws_url, additional_headers=self._handshake_headers()
        ) as ws:
            params: dict[str, Any] = {"channels": channels}
            if market_tickers:
                params["market_tickers"] = market_tickers
            subscribe = {
                "id": next(self._id_counter),
                "cmd": "subscribe",
                "params": params,
            }
            await ws.send(json.dumps(subscribe))
            async for raw in ws:
                yield json.loads(raw)
