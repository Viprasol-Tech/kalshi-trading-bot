"""Logging setup using Rich for readable console output.

Part of the Kalshi Trading Bot by Viprasol Tech Private Limited.
"""

from __future__ import annotations

import logging

from rich.logging import RichHandler

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Install a Rich console handler once."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger."""
    configure_logging()
    return logging.getLogger(name)
