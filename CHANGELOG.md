# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2025

### Added
- Three new example strategies: `MeanReversion` (Bollinger-style fade),
  `ArbitrageYesNo` (YES+NO < 100c lock-in), and `FairValue` (Kelly-sized edge).
- Backtest performance metrics module (`backtesting/metrics.py`): annualised
  Sharpe ratio, max drawdown, volatility, win rate and profit factor.
- `BacktestReport` with per-trade PnL accounting, fills log, fee modelling,
  optional risk-manager screening, and a `summary()` dict.
- Rich console report renderer (`backtesting/report.py`).
- Deterministic synthetic data generator (`backtesting/data.py`) for offline
  backtests and demos.
- Richer exchange models: `Fill`, `OrderBook` / `OrderBookLevel`, `Portfolio`,
  plus `Position.unrealized_pnl` and `Balance.balance_dollars` helpers.
- New CLI subcommands: `strategies` (list bundled strategies) and `backtest`
  (run a strategy on synthetic data and print a metrics report).
- Nested, env-overridable `RiskSettings` (`KALSHI_RISK__*`) wired into the CLI
  via `RiskManager.from_settings`; configurable engine `poll_interval`.
- `examples/backtest_demo.py` runnable script.

### Changed
- `Backtester.run` now returns the richer `BacktestReport` (`BacktestResult`
  remains as a backwards-compatible alias).
- API key IDs are whitespace-trimmed and core settings are bounds-validated.

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

[Unreleased]: https://github.com/Viprasol-Tech/kalshi-trading-bot/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Viprasol-Tech/kalshi-trading-bot/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Viprasol-Tech/kalshi-trading-bot/releases/tag/v0.1.0
