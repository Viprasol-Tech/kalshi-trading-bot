# Configuration

All settings are read from environment variables (prefixed `KALSHI_`) and from a
`.env` file. They are validated at startup by pydantic.

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | _(empty)_ | Must remain empty; credentials are rejected. |
| `KALSHI_PRIVATE_KEY_PATH` | `./secrets/kalshi_private_key.pem` | Legacy setting; no private key is loaded. |
| `KALSHI_ENVIRONMENT` | `demo` | Only `demo` is permitted. |
| `KALSHI_DRY_RUN` | `true` | Must be true; real order submission is blocked independently. |
| `KALSHI_REQUEST_TIMEOUT` | `10` | HTTP timeout in seconds. |

## Environments & base URLs

| Environment | REST base URL | WebSocket base URL |
|---|---|---|
| `demo` | `https://demo-api.kalshi.co/trade-api/v2` | Disabled on this branch |

Production URLs, credentialed requests, exchange portfolio access, and
authenticated WebSockets are rejected. See [simulation-only policy](simulation-only.md).

## Programmatic use

```python
from kalshi_bot.config import load_settings
from kalshi_bot.exchange.client import KalshiClient

settings = load_settings()
async with KalshiClient.from_settings(settings) as client:
    markets = await client.get_markets(status="open", limit=5)
```
