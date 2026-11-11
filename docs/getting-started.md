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

## Paper account only

```bash
KALSHI_ENVIRONMENT=demo KALSHI_DRY_RUN=true KALSHI_API_KEY_ID= kalshi-bot paper-status
kalshi-bot backtest momentum --ticks 200
```

Never configure exchange credentials on this branch. Production settings and
`--live` are rejected. Public demo snapshots missing executable quote metadata
cannot trigger paper entries. Read [simulation-only behavior](simulation-only.md).
