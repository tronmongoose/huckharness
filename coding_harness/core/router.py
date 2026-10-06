"""Routing-decision surface for the harness agent loop.

In a deployment this re-exports the real decision layer from
pipelines/clawrouter.py — resolved via cwd/PYTHONPATH, since the harness always
runs inside the deployment's checkout there (interactive from the repo root,
nightshift serve with cwd set to a worktree). Run standalone, with no deployment
repo on the path, it falls back to a local-only default so the harness never
reaches for a frontier model without an explicit deployment router. Same
fail-soft posture as core/cost.py.
"""
from __future__ import annotations

try:
    from pipelines.clawrouter import RouteDecision, decide_route
    from pipelines.common import generation_local_only
except ImportError:  # standalone: no deployment router on the path
    from dataclasses import dataclass, field

    @dataclass(frozen=True)
    class RouteDecision:  # the minimal shape the harness loop reads
        route: str = "local"
        model: str = ""
        route_reason: str = "no_router_local_default"
        sensitivity_flag: bool = False
        retrieval_flag: bool = False
        classification: dict = field(default_factory=dict)

    def decide_route(query, context=None):  # type: ignore[misc]
        """Standalone default: never leave the machine without a real router."""
        return RouteDecision()

    def generation_local_only():  # type: ignore[misc]
        """Standalone default: local-only, matching the safe deployment posture."""
        return True

__all__ = ["decide_route", "RouteDecision", "generation_local_only"]
