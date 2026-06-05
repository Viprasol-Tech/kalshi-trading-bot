# Getting Started

## Install

```bash
git clone https://github.com/Viprasol-Tech/kalshi-trading-bot.git
cd kalshi-trading-bot
python -m pip install -e ".[dev]"
cp .env.example .env
```

Python 3.11+ is required.

## First commands

Public market data needs no credentials:

```bash
kalshi-bot version
kalshi-bot markets --status open --limit 10
```

Dry-run a strategy (no real orders are sent):

```bash
kalshi-bot run momentum INXD-23DEC29-B5000 --ticks 5
```

## Add credentials

1. In the Kalshi dashboard go to **Profile → API Keys** and create a key. You
   receive an **API Key ID** and download an **RSA private key** once.
2. Save the key to `./secrets/kalshi_private_key.pem` (git-ignored).
3. Edit `.env`:

```dotenv
KALSHI_API_KEY_ID=your-key-id
KALSHI_PRIVATE_KEY_PATH=./secrets/kalshi_private_key.pem
KALSHI_ENVIRONMENT=demo
KALSHI_DRY_RUN=true
```

Now authenticated commands work:

```bash
kalshi-bot balance
```

## Going live

Live trading sends real orders. Only after thorough testing in `demo`:

```bash
kalshi-bot run momentum <TICKER> --live
```
