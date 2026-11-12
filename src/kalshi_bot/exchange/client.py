"""Unsigned, read-only demo Kalshi market-data client.

The final HTTP hook rejects production, credentials, private portfolio/history
routes and all writes. No private key is loaded and no request is signed.

Example:
    ```python
    from kalshi_bot.config import load_settings
    from kalshi_bot.exchange.client import KalshiClient

    settings = load_settings()
    async with KalshiClient.from_settings(settings) as client:
        markets = await client.get_markets(status="open", limit=10)
    ```

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_bot.config import Settings
from kalshi_bot.exchange.models import (
    Balance,
    Market,
    Order,
    OrderRequest,
    Position,
)
from kalshi_bot.safety import (
    SimulationOnlyError,
    require_public_demo_url,
    require_simulation,
)

API_PREFIX = "/trade-api/v2"


def public_read_path(path: str) -> bool:
    """Allow market data, never private historical orders/fills/positions."""
    return (
        path == "/markets"
        or path.startswith(("/markets/", "/series/", "/historical/markets/"))
        or path in ("/historical/cutoff", "/historical/markets")
    )


def validate_public_request(request: httpx.Request) -> None:
    """Reusable final-destination check for the campaign's raw HTTP adapter."""
    require_simulation()
    parsed = urlsplit(str(request.url))
    require_public_demo_url(f"{parsed.scheme}://{parsed.netloc}")
    if (
        request.method != "GET"
        or not parsed.path.startswith(API_PREFIX + "/")
        or not public_read_path(parsed.path.removeprefix(API_PREFIX))
        or any(
            k.lower().startswith(("authorization", "kalshi-access", "cookie"))
            for k in request.headers
        )
    ):
        raise SimulationOnlyError("Only unsigned public demo market reads are allowed.")


class KalshiError(RuntimeError):
    """Raised when the Kalshi API returns an error response."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Kalshi API error {status_code}: {body}")
        self.status_code = status_code
        self.body = body


class KalshiClient:
    """Async REST client for the Kalshi trading API."""

    def __init__(
        self,
        base_url: str,
        api_key_id: str | None = None,
        private_key: rsa.RSAPrivateKey | None = None,
        timeout: float = 10.0,
    ) -> None:
        require_simulation()
        require_public_demo_url(base_url)
        if api_key_id or private_key is not None:
            raise SimulationOnlyError("Simulation-only: exchange credentials are forbidden.")
        self._base_url = base_url.rstrip("/")
        self._api_key_id = api_key_id
        self._private_key = private_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            event_hooks={"request": [self._sign_request]},
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> KalshiClient:
        """Build a demo data client; reject credentials before accessing any key."""
        require_simulation(dry_run=settings.dry_run)
        if settings.api_key_id or settings.environment.value != "demo":
            raise SimulationOnlyError("Simulation-only: credentials and production are forbidden.")
        return cls(
            base_url=settings.rest_base_url,
            timeout=settings.request_timeout,
        )

    @property
    def authenticated(self) -> bool:
        """Whether credentials are present for signed requests."""
        return self._api_key_id is not None and self._private_key is not None

    async def _sign_request(self, request: httpx.Request) -> None:
        """Final network boundary: public demo market GETs only, never signed."""
        validate_public_request(request)

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        require_simulation()
        require_public_demo_url(self._base_url)
        if method.upper() != "GET" or not public_read_path(path):
            raise SimulationOnlyError("Simulation-only: exchange portfolio and writes are blocked.")
        # httpx joins relative paths to base_url; do not append the API prefix twice.
        suffix = path if self._base_url.endswith(API_PREFIX) else API_PREFIX + path
        response = await self._client.request(method, suffix, **kwargs)
        if response.is_error:
            raise KalshiError(response.status_code, response.text)
        return response.json()  # type: ignore[no-any-return]

    # --- Market data (public) -------------------------------------------

    async def get_markets(self, **params: Any) -> list[Market]:
        """List markets. Filter by ``status``, ``event_ticker``, ``series_ticker``, ``limit``."""
        data = await self._request("GET", "/markets", params=params)
        return [Market.model_validate(m) for m in data.get("markets", [])]

    async def get_market(self, ticker: str) -> Market:
        """Fetch a single market by ticker."""
        data = await self._request("GET", f"/markets/{ticker}")
        return Market.model_validate(data["market"])

    async def get_orderbook(self, ticker: str, depth: int | None = None) -> dict[str, Any]:
        """Fetch the order book for a market."""
        params = {"depth": depth} if depth is not None else {}
        data = await self._request("GET", f"/markets/{ticker}/orderbook", params=params)
        return data.get("orderbook", {})  # type: ignore[no-any-return]

    # --- Portfolio / trading (authenticated) ----------------------------

    async def get_balance(self) -> Balance:
        """Get the portfolio cash balance (in cents)."""
        data = await self._request("GET", "/portfolio/balance")
        return Balance.model_validate(data)

    async def get_positions(self, **params: Any) -> list[Position]:
        """List open positions."""
        data = await self._request("GET", "/portfolio/positions", params=params)
        return [Position.model_validate(p) for p in data.get("market_positions", [])]

    async def create_order(self, order: OrderRequest) -> Order:
        """Create an order (``POST /portfolio/orders``)."""
        raise SimulationOnlyError("Real order submission is permanently disabled on this branch.")

    async def cancel_order(self, order_id: str) -> Order:
        """Cancel an order by ID (``DELETE /portfolio/orders/{id}``)."""
        raise SimulationOnlyError("Exchange order mutations are permanently disabled.")

    async def get_orders(self, **params: Any) -> list[Order]:
        """List orders, optionally filtered by ``ticker``/``status``."""
        data = await self._request("GET", "/portfolio/orders", params=params)
        return [Order.model_validate(o) for o in data.get("orders", [])]

    # --- Lifecycle ------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> KalshiClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
