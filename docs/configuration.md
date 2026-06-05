# Configuration

All settings are read from environment variables (prefixed `KALSHI_`) and from a
`.env` file. They are validated at startup by pydantic.

| Variable | Default | Description |
|---|---|---|
| `KALSHI_API_KEY_ID` | _(empty)_ | API key ID (UUID). Required for authenticated calls. |
| `KALSHI_PRIVATE_KEY_PATH` | `./secrets/kalshi_private_key.pem` | Path to the RSA private key PEM file. |
| `KALSHI_ENVIRONMENT` | `demo` | `demo` (sandbox) or `prod` (live). |
| `KALSHI_DRY_RUN` | `true` | Simulate orders instead of sending them. |
| `KALSHI_REQUEST_TIMEOUT` | `10` | HTTP timeout in seconds. |

## Environments & base URLs

| Environment | REST base URL | WebSocket base URL |
|---|---|---|
| `prod` | `https://api.elections.kalshi.com/trade-api/v2` | `wss://api.elections.kalshi.com/trade-api/ws/v2` |
| `demo` | `https://demo-api.kalshi.co/trade-api/v2` | `wss://demo-api.kalshi.co/trade-api/ws/v2` |

!!! note "Host naming"
    Kalshi has referenced both `api.elections.kalshi.com` (used by the official
    starter code) and `external-api.kalshi.com` (newer docs) for production. This
    framework defaults to the battle-tested starter-code hosts and exposes the
    base URLs in `kalshi_bot/config.py` so you can override them if Kalshi
    migrates.

## Programmatic use

```python
from kalshi_bot.config import load_settings
from kalshi_bot.exchange.client import KalshiClient

settings = load_settings()
async with KalshiClient.from_settings(settings) as client:
    markets = await client.get_markets(status="open", limit=5)
```
