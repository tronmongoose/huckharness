"""Brain tool: search the second brain (the user's note index) from a turn.

Read-only and plan-safe. Hits above the tier this turn may carry are withheld
and counted, never passed through: all tiers on local Ollama, internal and
below on a frontier backend (``context.brain.allowed_tier``). The session sets
``local`` per model step and reads ``metadata["max_tier"]`` to mark its
history sensitive once confidential material enters it. A stale index still
answers, under a first line that says how old it is.
"""
from __future__ import annotations

from typing import Any

from coding_harness.context.brain import (
    BrainClient,
    BrainError,
    allowed_tier,
    is_stale,
    tier_of,
)
from coding_harness.context.brain_inprocess import UPGRADE_WARNING

from .base import Tool, ToolResult

TOP_K = 8


class Brain(Tool):
    name = "Brain"
    category = "read"
    description = (
        "Search the user's second brain: their notes, journals and project memory. "
        "Returns the best-matching note excerpts with their paths. Use it when the "
        "task depends on something the user has written down before."
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for, in plain words."},
            "vault": {"type": "string",
                      "description": "Optional vault to search, e.g. 'startup' or 'finance'."},
        },
        "required": ["query"],
    }

    def __init__(self, client: BrainClient):
        self.client = client
        self.local = True  # set by the session before each model step

    def _age(self) -> float | None:
        """The index age; a fault here never costs the search its answer."""
        try:
            return self.client.age_hours()
        except BrainError:
            return None

    def run(self, args: dict[str, Any]) -> ToolResult:
        """Search, then drop hits above this turn's tier and say how many."""
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            return ToolResult("query must be a non-empty string", is_error=True)
        vault = args.get("vault") if isinstance(args.get("vault"), str) else None
        try:
            hits = self.client.search(query, vault=vault or None, top_k=TOP_K)
        except BrainError as e:
            return ToolResult(f"brain unavailable: {e}", is_error=True)
        cap = allowed_tier(self.local)
        shown = [h for h in hits if tier_of(h.sensitivity) <= cap]
        withheld = len(hits) - len(shown)
        lines = [f"## {h.path} ({h.sensitivity or 'unlabeled'})\n{h.snippet.strip()}" for h in shown]
        if withheld:
            lines.append(f"({withheld} more result(s) withheld: above the tier a "
                         f"{'local' if self.local else 'frontier'} turn may carry.)")
        if not lines:
            lines.append("No matching notes.")
        age = self._age()
        if is_stale(age):
            lines.insert(0, f"Note: the index is {round(age or 0)} hours old.")
        if UPGRADE_WARNING in self.client.last_warnings:
            lines.insert(0, f"Note: {UPGRADE_WARNING}.")
        max_tier = max((tier_of(h.sensitivity) for h in shown), default=0)
        return ToolResult("\n\n".join(lines),
                          metadata={"max_tier": max_tier, "withheld": withheld})
