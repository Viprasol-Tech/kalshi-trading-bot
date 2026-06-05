# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2025

### Added
- Kalshi RSA-PSS request signing (`exchange/auth.py`).
- Async REST client: markets, order book, balance, positions, order create/cancel.
- WebSocket client for real-time channels (`orderbook_delta`, `ticker`, `trade`, `fill`).
- Pluggable `Strategy` base class with `MarketMaker` and `Momentum` examples.
- Risk manager with pre-trade limits and fractional-Kelly position sizing.
- Snapshot-replay backtester with summary metrics.
- Typer CLI (`markets`, `balance`, `run`) with dry-run by default.
- Typed configuration via pydantic-settings (demo/prod environments).
- Tooling: ruff, mypy (strict), pytest, pre-commit, Docker, GitHub Actions CI, mkdocs.

[Unreleased]: https://github.com/Viprasol-Tech/kalshi-trading-bot/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Viprasol-Tech/kalshi-trading-bot/releases/tag/v0.1.0
