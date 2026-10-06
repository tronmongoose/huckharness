"""Lifecycle hooks: settings-declared shell commands run on session events.

Exports ``EVENTS``, ``DEFAULT_TIMEOUT_S``, ``HookDecision``, ``HookRunner``
and ``build_runner``. Contract per hook: JSON payload on stdin (``event``,
``session_id``, ``cwd``, plus ``tool_name``/``tool_input``/``tool_output``
on tool events), exit 0 allows, exit 2 blocks with the reason on stderr, and
a stdout JSON object ``{"decision": "block", "reason": ...}`` also blocks.
A PreToolUse block denies the tool call and fails closed on a timeout or
crash; every other event fails open and logs. A Stop block's reason is
collected on the decision so the caller can feed it back as one user
message. ``HARNESS_HOOKS=0`` disables everything.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

from coding_harness.security.env_scrub import scrub

EVENTS = (
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "PreCompact", "Stop",
)
DEFAULT_TIMEOUT_S = 10
_OUTPUT_CAP = 8000
_TOOL_MATCHER_RE = re.compile(r"^([A-Za-z_][\w-]*)\((.*)\)$")

LogFn = Callable[[str, "dict[str, Any]"], None]


@dataclass
class HookDecision:
    """What the hooks for one event decided."""

    allowed: bool = True
    reason: str = ""
    stop_messages: list[str] = field(default_factory=list)


def _stderr_log(kind: str, payload: dict[str, Any]) -> None:
    """Default log sink: one JSONL line on stderr, never raises."""
    try:
        line = json.dumps({"event": kind, **payload}, default=str)
        print(line, file=sys.stderr, flush=True)
    except Exception:  # noqa: BLE001 telemetry must not break the turn
        pass


def build_runner(
    settings: Any, *, session_id: str, cwd: str, log: LogFn | None = None,
) -> HookRunner | None:
    """A runner for ``settings.hooks``; None when disabled or empty."""
    if os.environ.get("HARNESS_HOOKS") == "0":
        return None
    hooks = getattr(settings, "hooks", None)
    if not isinstance(hooks, dict) or not hooks:
        return None
    return HookRunner(hooks, session_id=session_id, cwd=cwd, log=log)


def _entries(raw: Any, log: LogFn) -> list[tuple[str, str, float]]:
    """(matcher, command, timeout) triples from one event's config list.

    Accepts the flat form ``{matcher, command, timeout}`` and Claude Code's
    nested form ``{matcher, hooks: [{command, timeout}]}`` so an existing
    settings.json block can be pasted in unchanged. Bad entries are skipped
    and logged, never fatal.
    """
    out: list[tuple[str, str, float]] = []
    if not isinstance(raw, list):
        log("hook_config_error", {"error": "event value must be a list"})
        return out
    for entry in raw:
        if not isinstance(entry, dict):
            log("hook_config_error", {"error": f"not an object: {entry!r}"})
            continue
        matcher = entry.get("matcher", "") or ""
        inner = entry.get("hooks")
        specs = inner if isinstance(inner, list) else [entry]
        for spec in specs:
            command = spec.get("command") if isinstance(spec, dict) else None
            if not isinstance(command, str) or not command:
                log("hook_config_error", {"error": f"no command: {spec!r}"})
                continue
            timeout = spec.get("timeout", DEFAULT_TIMEOUT_S)
            if not isinstance(timeout, (int, float)) or timeout <= 0:
                timeout = DEFAULT_TIMEOUT_S
            out.append((str(matcher), command, float(timeout)))
    return out


def _matches(matcher: str, tool_name: str | None, tool_input: dict[str, Any] | None) -> bool:
    """Whether a matcher selects this call; empty matches all, non-tool
    events ignore matchers entirely."""
    if not matcher or tool_name is None:
        return True
    m = _TOOL_MATCHER_RE.match(matcher)
    if m is not None:
        if tool_name != m.group(1):
            return False
        command = str((tool_input or {}).get("command", ""))
        return fnmatch.fnmatchcase(command, m.group(2))
    return fnmatch.fnmatchcase(tool_name, matcher)


def _blocked(proc: subprocess.CompletedProcess) -> tuple[bool, str]:
    """(blocked, reason) from an exit-2 stderr or a stdout block decision."""
    if proc.returncode == 2:
        return True, (proc.stderr or "").strip() or "blocked by hook"
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        return False, ""
    if isinstance(data, dict) and data.get("decision") == "block":
        return True, str(data.get("reason") or "blocked by hook")
    return False, ""


class HookRunner:
    """Runs every configured hook for an event and folds the verdicts."""

    def __init__(
        self,
        hooks: dict[str, Any],
        *,
        session_id: str,
        cwd: str,
        log: LogFn | None = None,
    ) -> None:
        self.hooks = hooks
        self.session_id = session_id
        self.cwd = cwd
        self.log = log or _stderr_log

    def run(
        self,
        event: str,
        *,
        tool_name: str | None = None,
        tool_input: dict[str, Any] | None = None,
        tool_output: str | None = None,
    ) -> HookDecision:
        """Run ``event``'s hooks in order; the first PreToolUse block wins."""
        decision = HookDecision()
        for matcher, command, timeout in _entries(self.hooks.get(event, []), self.log):
            if not _matches(matcher, tool_name, tool_input):
                continue
            self._run_one(event, command, timeout, decision,
                          tool_name, tool_input, tool_output)
            if not decision.allowed:
                break
        return decision

    def _run_one(  # LONG-FN: one subprocess plus the full verdict fold
        self,
        event: str,
        command: str,
        timeout: float,
        decision: HookDecision,
        tool_name: str | None,
        tool_input: dict[str, Any] | None,
        tool_output: str | None,
    ) -> None:
        payload: dict[str, Any] = {
            "event": event, "session_id": self.session_id, "cwd": self.cwd,
        }
        if tool_name is not None:
            payload["tool_name"] = tool_name
        if tool_input is not None:
            payload["tool_input"] = tool_input
        if tool_output is not None:
            payload["tool_output"] = tool_output[:_OUTPUT_CAP]
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                input=json.dumps(payload, default=str),
                text=True,
                capture_output=True,
                timeout=timeout,
                cwd=self.cwd,
                env=scrub(os.environ),
            )
        except subprocess.TimeoutExpired:
            self._fail(event, decision, f"hook timed out (>{timeout:g}s)")
            return
        except OSError as e:
            self._fail(event, decision, f"hook could not be invoked: {e}")
            return
        blocked, reason = _blocked(proc)
        if blocked:
            if event == "PreToolUse":
                decision.allowed = False
                decision.reason = reason
            elif event == "Stop":
                decision.stop_messages.append(reason)
            else:
                self.log("hook_block_ignored", {"hook_event": event, "reason": reason})
        elif proc.returncode != 0:
            self._fail(
                event, decision,
                f"hook exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}",
            )

    def _fail(self, event: str, decision: HookDecision, reason: str) -> None:
        """Timeout or crash: PreToolUse fails closed, everything else logs."""
        self.log("hook_error", {"hook_event": event, "error": reason})
        if event == "PreToolUse":
            decision.allowed = False
            decision.reason = reason
