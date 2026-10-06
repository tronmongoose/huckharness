"""Stuck-call detector for the agent loop.

Small models re-issue the identical tool call (same Grep, same failing Edit)
until MAX_TURNS runs out, at a minute a step. A step whose every call has the
same canonical key as a call in the previous step, with no mutation between,
is a repeat: two identical steps earn a nudge, four halt the prompt. A
successful Write/Edit/Bash resets the count because the world changed.

Kill switch: HARNESS_STUCK_DETECT=0 makes ``observe`` always return None.

Exports: canonical, StepTracker, nudge_text, HALT_TEXT, NUDGE_AFTER,
HALT_AFTER, enabled.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

NUDGE_AFTER = 2
HALT_AFTER = 4

nudge_text = (
    "You already ran this exact call and the result will not change. "
    "Take a different approach: read a different file, change the search, "
    "or edit the code."
)
HALT_TEXT = f"(halted: repeated the same call {HALT_AFTER} times without progress)"

_WS = re.compile(r"\s+")


def enabled() -> bool:
    """Whether the detector is on (HARNESS_STUCK_DETECT, default on)."""
    return os.environ.get("HARNESS_STUCK_DETECT", "1") != "0"


def _normalize(value: Any) -> Any:
    """Collapse runs of whitespace in every string, recursing through containers."""
    if isinstance(value, str):
        return _WS.sub(" ", value).strip()
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def canonical(name: str, args: dict[str, Any]) -> str:
    """Stable key for a tool call: name plus sorted, whitespace-normalized args."""
    return json.dumps([name, _normalize(args)], sort_keys=True, separators=(",", ":"))


class StepTracker:
    """Counts consecutive identical steps for one prompt."""

    def __init__(self) -> None:
        self._enabled = enabled()
        # key -> is_error for the previous step; a call that failed before and
        # succeeds now is not a repeat, the world moved.
        self._prev: dict[str, bool] = {}
        self._run = 0

    @property
    def run(self) -> int:
        """Length of the current run of identical steps."""
        return self._run

    def observe(
        self, step: list[tuple[str, str]], errors: list[bool], mutated: bool,
    ) -> str | None:
        """Record one step's (name, key) calls; return None, 'nudge' or 'halt'."""
        if not self._enabled:
            return None
        if mutated or not step:
            self._prev = {}
            self._run = 0
            return None
        current = {key: err for (_name, key), err in zip(step, errors)}
        repeat = bool(self._prev) and all(
            key in self._prev and self._prev[key] == err
            for key, err in current.items()
        )
        self._run = self._run + 1 if repeat else 1
        self._prev = current
        if self._run >= HALT_AFTER:
            return "halt"
        if self._run >= NUDGE_AFTER:
            return "nudge"
        return None
