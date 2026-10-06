"""Recovery from malformed model output in the agent loop.

Two failure shapes a small model produces that the loop used to swallow:
a reply cut off at the output limit (finish_reason 'length') whose tool
call is therefore incomplete, and tool-call arguments that are not valid
JSON. Both are answered with a tool error in place of the dispatch, so the
transcript keeps one result per call and rewind's user-turn count holds.

Exports: TRUNCATED_TEXT, INVALID_ARGS_TEXT, MAX_NUM_PREDICT,
is_truncated_call, doubled_num_predict, parse_tool_args, audit_invalid_args.
"""
from __future__ import annotations

import json
from typing import Any

from coding_harness.security import audit

MAX_NUM_PREDICT = 8192

TRUNCATED_TEXT = (
    "Your response was cut off at the output limit before the tool call "
    "completed. Write smaller pieces: use Edit for changes, or split a large "
    "Write into several."
)
INVALID_ARGS_TEXT = (
    "arguments were not valid JSON: {error}; resend the call with valid JSON"
)


def is_truncated_call(msg: dict[str, Any], usage: dict[str, Any]) -> bool:
    """True when the reply hit the output limit while carrying tool calls."""
    return usage.get("finish_reason") == "length" and bool(msg.get("tool_calls"))


def doubled_num_predict(current: int) -> int:
    """Twice the current output budget, capped at MAX_NUM_PREDICT."""
    return min(MAX_NUM_PREDICT, current * 2)


def parse_tool_args(raw: Any) -> tuple[dict[str, Any], str | None]:
    """Decode a tool call's arguments; the second value is the JSON error, if any."""
    if not isinstance(raw, str):
        return (raw or {}), None
    if not raw:
        return {}, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as e:
        return {}, str(e)


def audit_invalid_args(
    *, session_id: str, tool: str, raw_args: str, agent_identity: str | None,
) -> dict[str, Any]:
    """Append the audit row for a call rejected before dispatch; returns the entry."""
    return audit.append(
        session_id=session_id,
        tool=tool,
        args={"_raw": raw_args},
        result=None,
        allowed=False,
        sentinel_reason="invalid_args",
        sentinel_path="registry",
        error="invalid_args",
        agent_identity=agent_identity,
    )
