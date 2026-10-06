"""Thin import adapter so harness code never reaches into pipelines/.

Mirrors core/router.py: the harness imports the spend-governance surface from
here, and pipelines/_cost_log.py stays the single source of truth for the
daily/monthly caps and the api-spend ledger. This unifies harness frontier
spend (interactive + nightshift) with the pipeline path, which already routes
every Anthropic call through _cost_log.

Fail-soft: if pipelines/ is absent (harness run standalone), under_cap() allows
and record() is a no-op — the same posture as common.py's lazy import.
"""
try:
    # Resolved via cwd/PYTHONPATH in a deployment; absent standalone.
    from pipelines._cost_log import record, under_cap
except ImportError:  # harness running without the deployment repo
    def under_cap():  # type: ignore[misc]
        """No-op fallback: allow when the cost log is unavailable."""
        return True, None

    def record(model, usage, cost_usd, elapsed_s=None):  # type: ignore[misc]
        """No-op fallback: drop the record when the cost log is unavailable."""
        return None

# USD per 1M tokens (input, output). Authoritative rates per the claude-api
# skill model table (cached 2026-05-26): Opus 5/25, Sonnet 3/15, Haiku 1/5.
# Resolved by substring so date-stamped ids (claude-sonnet-4-20250514) match —
# NOT sourced from claude_cost_tracker.PRICING, which is stale for opus/haiku.
_RATES = {
    "haiku": (1.00, 5.00),
    "sonnet": (3.00, 15.00),
    "opus": (5.00, 25.00),
}
_DEFAULT_RATE = (3.00, 15.00)  # unknown frontier id → bill at Sonnet (conservative-ish)


def cost_for(model: str, tokens_in: int, tokens_out: int) -> float:
    """Estimate USD for one Anthropic call. Substring match on the model id.

    The harness sends no cache_control, so its usage carries no cache discount —
    this is a full-rate upper bound, which is correct for a circuit breaker.
    """
    m = (model or "").lower()
    rate_in, rate_out = _DEFAULT_RATE
    for key, rates in _RATES.items():
        if key in m:
            rate_in, rate_out = rates
            break
    return (tokens_in * rate_in + tokens_out * rate_out) / 1_000_000


__all__ = ["under_cap", "record", "cost_for"]
