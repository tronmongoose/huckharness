"""Context-budget guards for the agent loop.

Exports: enabled, token_budget, estimate_tokens, clamp_tool_result,
elide_superseded_reads, TOOL_CAPS, UNCAPPED, DEFAULT_CAP, ELIDED_TEMPLATE.
Kill switch: HARNESS_CONTEXT_BUDGET=0 disables clamping, eliding and the
in-loop budget check together.
"""
from __future__ import annotations

import json
import os
from typing import Any

TOOL_CAPS = {"Grep": 8_000, "Glob": 4_000}
# Read rides uncut (the model asked for exactly that content); Bash output is
# already middle-out capped at the source (tools/bash.py MAX_OUTPUT).
UNCAPPED = ("Read", "Bash")
DEFAULT_CAP = 8_000  # MCP tools and anything else unlisted
_HEAD_SHARE = 0.6

BUDGET_SHARE = 0.6
RESERVE_TOKENS = 1_500
BUDGET_FLOOR_DIVISOR = 4  # the floor wins only below about 4.3k tokens of num_ctx
# Serialized JSON runs close to 4 chars per token on code-heavy history.
CHARS_PER_TOKEN = 4

ELIDED_TEMPLATE = "[content elided: superseded by a later Read/Edit of {path}]"
# How a successful result opens, per tool. Only a success supersedes: a failed
# Edit leaves the file as the earlier Read showed it. An elided stub counts,
# since its Read succeeded before it was superseded.
_OK_PREFIXES = {
    "Read": ("# ", "[content elided"),
    "Edit": ("edited ",),
    "Write": ("created ", "overwrote ", "wrote "),
}


def enabled() -> bool:
    """Whether the context-budget machinery is on (HARNESS_CONTEXT_BUDGET=0 kills it)."""
    return os.environ.get("HARNESS_CONTEXT_BUDGET", "1") != "0"


def token_budget(num_ctx: int) -> int:
    """Prompt-token ceiling: 60% of num_ctx minus a reply reserve, never under a quarter of it.

    The floor keeps a small window from getting a zero or negative budget,
    which would shrink the history on every step.
    """
    return max(int(BUDGET_SHARE * num_ctx) - RESERVE_TOKENS, num_ctx // BUDGET_FLOOR_DIVISOR)


def estimate_tokens(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
) -> int:
    """Rough prompt size in tokens: serialized messages plus the tools schema."""
    chars = sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)
    if tools:
        chars += len(json.dumps(tools, ensure_ascii=False, default=str))
    return chars // CHARS_PER_TOKEN


def clamp_tool_result(name: str, content: str, cap: int | None = None) -> str:
    """Middle-out cut of one tool result to its per-tool cap, with a marker naming the cut."""
    if not enabled():
        return content
    if cap is None:
        if name in UNCAPPED:
            return content
        cap = TOOL_CAPS.get(name, DEFAULT_CAP)
    if len(content) <= cap:
        return content
    marker = (
        f"\n... [{name} result clamped: {len(content) - cap} chars "
        "cut from the middle] ...\n"
    )
    budget = max(0, cap - len(marker))
    head = int(budget * _HEAD_SHARE)
    return content[:head] + marker + content[len(content) - (budget - head):]


def _call_key(call: dict[str, Any]) -> tuple[str, str, str]:
    """(tool name, file_path or "", read range) for one assistant tool call."""
    fn = call.get("function") or {}
    raw = fn.get("arguments") or "{}"
    try:
        args = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        args = {}
    if not isinstance(args, dict):
        args = {}
    path = args.get("file_path")
    span = f"{args.get('offset')}:{args.get('limit')}"
    return fn.get("name", ""), path if isinstance(path, str) else "", span


def _succeeded(name: str, content: Any) -> bool:
    """Whether a Read/Edit/Write result reads as a success."""
    return isinstance(content, str) and content.startswith(_OK_PREFIXES.get(name, ()))


def _paired_tool_events(
    messages: list[dict[str, Any]],
) -> list[tuple[int, str, str, str]]:
    """(tool-result index, tool name, file_path, read range) for successful path-bearing calls.

    Pairing is positional: the session appends exactly one tool message per
    tool_call, in order, on every path (dispatch, rejects, denials), so this
    holds on a live history and on a replayed one identically. A user message
    or a reply without calls ends the step, so leftover calls never pair.
    """
    events: list[tuple[int, str, str, str]] = []
    pending: list[tuple[str, str, str]] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            pending = [_call_key(call) for call in m.get("tool_calls") or []]
        elif role == "tool" and pending:
            name, path, span = pending.pop(0)
            if path and _succeeded(name, m.get("content")):
                events.append((i, name, path, span))
        elif role != "tool":
            pending = []
    return events


def _superseded(events: list[tuple[int, str, str, str]]) -> list[tuple[int, str]]:
    """(index, path) of each Read made stale by a later Edit/Write of its path,
    or by a later Read of the same path and range."""
    written: set[str] = set()
    read_again: set[tuple[str, str]] = set()
    stale: list[tuple[int, str]] = []
    for i, name, path, span in reversed(events):
        if name == "Read":
            if path in written or (path, span) in read_again:
                stale.append((i, path))
            read_again.add((path, span))
        elif name in ("Edit", "Write"):
            written.add(path)
    return stale


def elide_superseded_reads(messages: list[dict[str, Any]]) -> int:
    """Replace earlier Read results that a later Read or Edit/Write made stale.

    Mutates the tool messages in place, swapping each superseded Read's
    content for a one-line marker. A later Read of a different range of the
    same file does not supersede. Returns how many were newly elided.
    Deterministic over a message list, so replay reproduces it exactly.
    """
    count = 0
    for i, path in _superseded(_paired_tool_events(messages)):
        replacement = ELIDED_TEMPLATE.format(path=path)
        if messages[i].get("content") != replacement:
            messages[i]["content"] = replacement
            count += 1
    return count
