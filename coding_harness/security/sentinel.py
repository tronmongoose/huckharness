"""Sentinel facade: the single ``review`` the registry calls and tests patch.

Exports ``SentinelVerdict`` (re-exported from ``security.policy``),
``BUILTIN_CATEGORIES`` and ``review``. The default path is the in-process
``policy.review``, which consults the deployment hook (if one resolves above
cwd) after its own rules. ``HARNESS_POLICY=hook`` is the emergency rollback:
only the legacy subprocess hook runs, with the bundled default allowed as the
fallback, and a missing hook fails closed.

The hook itself still logs to its own JSONL and alerts on block; the harness's
hash-chained audit row is written separately by ``security.audit.append``.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from coding_harness.security import hook_adapter, policy
from coding_harness.security.policy import SentinelVerdict

if TYPE_CHECKING:
    from coding_harness.core.mode import Autonomy
    from coding_harness.core.settings import Settings

__all__ = ["BUILTIN_CATEGORIES", "SentinelVerdict", "review"]

BUILTIN_CATEGORIES = {
    "Read": "read",
    "Grep": "read",
    "Glob": "read",
    "Write": "edit",
    "Edit": "edit",
    "Bash": "execute",
}


def review(
    tool_name: str,
    tool_input: dict[str, Any],
    session_id: str,
    *,
    category: str | None = None,
    cwd: str | None = None,
    level: Autonomy | None = None,
    settings: Settings | None = None,
) -> SentinelVerdict:
    """Review one tool call; category defaults from the built-in table."""
    if category is None:
        category = BUILTIN_CATEGORIES.get(tool_name)
    if cwd is None:
        cwd = os.getcwd()
    if os.environ.get("HARNESS_POLICY") == "hook":
        return _hook_only(tool_name, category, tool_input, session_id, cwd)
    return policy.review(
        tool_name, category, tool_input, session_id,
        cwd=cwd, level=level, settings=settings,
    )


def _hook_only(
    tool_name: str,
    category: str | None,
    tool_input: dict[str, Any],
    session_id: str,
    cwd: str,
) -> SentinelVerdict:
    """Legacy path: the subprocess hook decides alone, fail closed when absent."""
    verdict = hook_adapter.review(
        tool_name, category, tool_input, session_id, cwd=cwd, allow_bundled=True,
    )
    if verdict is None:
        return SentinelVerdict(
            False, "HARNESS_POLICY=hook but no sentinel-gate hook found", policy.PATH_HOOK,
        )
    return SentinelVerdict(verdict.allowed, verdict.reason, policy.PATH_HOOK)
