"""Non-configurable simulation-only execution boundary.

This branch intentionally has no real-trading escape hatch.
"""

from urllib.parse import urlsplit


class SimulationOnlyError(RuntimeError):
    """An operation conflicts with the simulation-only policy."""


def require_simulation(*, dry_run: bool = True) -> None:
    """Reject unsafe environment overrides even for direct library callers."""
    import os

    if not dry_run or os.environ.get("KALSHI_DRY_RUN", "true").lower() != "true":
        raise SimulationOnlyError("Simulation-only: KALSHI_DRY_RUN must be true.")
    if os.environ.get("KALSHI_ENVIRONMENT", "demo").lower() != "demo":
        raise SimulationOnlyError("Simulation-only: only the demo environment is allowed.")
    if os.environ.get("KALSHI_API_KEY_ID", "").strip():
        raise SimulationOnlyError("Simulation-only: exchange credentials are forbidden.")


def require_public_demo_url(url: str) -> None:
    """Do not send requests to production or arbitrary credential-collecting hosts."""
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "demo-api.kalshi.co"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
        or parsed.path.rstrip("/") not in ("", "/trade-api/v2")
        or parsed.query
        or parsed.fragment
    ):
        raise SimulationOnlyError("Simulation-only: only public demo REST data is allowed.")
