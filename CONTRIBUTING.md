# Contributing to Kalshi Trading Bot

Thanks for your interest in contributing! This project is maintained by
[Viprasol Tech](https://viprasol.com). Contributions of all kinds are welcome —
bug reports, strategies, docs, and code.

## Development setup

```bash
git clone https://github.com/Viprasol-Tech/kalshi-trading-bot.git
cd kalshi-trading-bot
python -m pip install -e ".[dev]"
pre-commit install
```

## Before you open a PR

Run the full local check suite — CI runs the same:

```bash
ruff check .
ruff format --check .
mypy src
pytest
```

## Adding a strategy

1. Create a module under `src/kalshi_bot/strategies/examples/` (or your own package).
2. Subclass `kalshi_bot.strategies.base.Strategy` and implement `on_market_data`.
3. Add a unit test under `tests/` (a backtest is ideal).
4. Register it in `kalshi_bot.cli.STRATEGIES` if it should be runnable from the CLI.

## Guidelines

- Keep public functions typed and documented; `mypy` runs in `strict` mode.
- Match the style of surrounding code; `ruff format` is the source of truth.
- **Never** commit secrets, API keys, `.env`, or private key files.
- Trading code must default to **dry-run**; live order paths require explicit opt-in.
- Update `CHANGELOG.md` for user-facing changes.

## Branching & commits

- Branch off `main`; open PRs against `main`.
- Write clear, present-tense commit messages.

## Code of Conduct

By participating you agree to our [Code of Conduct](CODE_OF_CONDUCT.md).

## Questions

Reach us on [Telegram](https://t.me/viprasol_help) or at
[support@viprasol.com](mailto:support@viprasol.com).
