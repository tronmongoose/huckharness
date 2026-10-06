"""Rebuild a session's message list from its JSONL transcript.

Serve mode holds live sessions in an in-memory dict, so a restart orphans
every session even though its full transcript is on disk. This module turns
that transcript back into the ``_messages`` list a ``Session`` runs on, so a
session can outlive the process that started it.

Replay is a straight chronological pass over the transcript:

    user_message      → {"role": "user"}
    feedback_message  → {"role": "user"}, a done-gate or reviewer repair
                        request inside the same prompt; not a user turn
    steer_message     → {"role": "user", "_steer": True}, an operator message
                        injected mid-turn; not a user turn
    assistant_message → {"role": "assistant", content, tool_calls}
    tool_result       → {"role": "tool", tool_call_id, name, content}
    stuck_nudge       → appended to the preceding tool result's content,
                        where the live session put it after logging it
    compaction        → collapse to [system, summary, *tail], mirroring
                        ``compaction.compact`` so a resumed session inherits
                        the same trimmed history the live one had; a
                        ``mid_turn`` record keeps the turn's task message
                        too, mirroring ``compaction.compact_steps``
    context_elide     → re-run ``elide_superseded_reads``, which is
                        deterministic over the message list

Two deliberate divergences from the original session, both recorded in
``ReplayResult.warnings``:

* **The system prompt is supplied fresh by the caller**, not replayed. It
  carries the repo map and AGENTS.md context, which should reflect the repo
  as it is now, not as it was when the session started.
* **Transcripts written before tool results were logged** (harness < sl-a8kf)
  cannot be replayed faithfully — their assistant turns have unanswered tool
  calls. Those are reported ``complete=False`` and must not be resumed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_harness.core import stuck
from coding_harness.core.compaction import SUMMARY_MARKER
from coding_harness.core.context_budget import elide_superseded_reads


@dataclass
class ReplayResult:
    """Outcome of a transcript replay."""

    messages: list[dict[str, Any]]
    session_id: str = ""
    model: str = ""
    mode: str = ""
    agent_identity: str | None = None
    envelope: dict[str, Any] | None = None
    user_turns: int = 0
    complete: bool = True
    # Confidential notes entered this history; a resumed session stays local.
    sensitive: bool = False
    warnings: list[str] = field(default_factory=list)


def read_records(path: Path) -> list[dict[str, Any]]:
    """Parse a transcript into records, skipping unparseable lines.

    A truncated final line is expected after a hard kill — the whole point of
    resume — so a bad line is skipped, never fatal.
    """
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _pair_call_ids(assistant_msg: dict[str, Any], results: list[dict[str, Any]]) -> None:
    """Backfill ids onto tool_calls the model emitted without one.

    ``Session`` generates a call id when the model omits it, so the id lives on
    the tool_result record while the logged assistant tool_call has none.
    Pairing is positional — the session dispatches calls in order.
    """
    calls = assistant_msg.get("tool_calls") or []
    for call, result in zip(calls, results):
        if not call.get("id") and result.get("tool_call_id"):
            call["id"] = result["tool_call_id"]


def _apply_compaction(messages: list[dict[str, Any]], record: dict[str, Any],
                      warnings: list[str], anchor: int | None = None) -> list[dict[str, Any]]:
    """Reproduce a compaction event against the replayed messages.

    Mirrors ``compaction.compact``: keep the system message, insert the summary
    as an assistant turn, keep the verbatim tail. Tail length is derived from
    the recorded ``messages_after`` (system + summary + tail). A ``mid_turn``
    record also keeps the turn's prompt at ``anchor``, as ``compact_steps`` did.
    """
    after = record.get("messages_after")
    summary = record.get("summary") or ""
    head = [messages[0]] if messages else []
    if record.get("mid_turn"):
        if anchor is None or not 0 < anchor < len(messages):
            anchor = next((i for i in range(len(messages) - 1, 0, -1)
                           if messages[i].get("role") == "user"), None)
        if anchor is not None:
            head.append(messages[anchor])
    if not isinstance(after, int) or after < len(head) + 1 or not messages:
        warnings.append("compaction record unusable; history left uncompacted")
        return messages
    tail_len = after - len(head) - 1
    tail = messages[len(messages) - tail_len:] if tail_len > 0 else []
    return [
        *head,
        {"role": "assistant", "content": f"{SUMMARY_MARKER}\n{summary}"},
        *tail,
    ]


def rebuild_messages(path: Path, system_prompt: str) -> ReplayResult:
    """Rebuild a runnable message list from a transcript. LONG-FN: single
    chronological pass; splitting it would hide the record ordering it encodes."""
    records = read_records(path)
    warnings: list[str] = []
    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    result = ReplayResult(messages=messages, warnings=warnings)
    pending: dict[str, Any] | None = None  # assistant msg awaiting its results
    pending_results: list[dict[str, Any]] = []
    saw_tool_call = saw_tool_result = False
    anchor: int | None = None  # index of the current turn's prompt, for mid-turn compaction

    for rec in records:
        kind = rec.get("kind")
        if kind == "session_start":
            result.session_id = rec.get("session_id", "")
            result.model = rec.get("model", "")
            result.mode = rec.get("mode", "")
            result.agent_identity = rec.get("identity")
            result.envelope = rec.get("envelope")
        elif kind == "user_message":
            pending = None
            pending_results = []
            anchor = len(messages)
            messages.append({"role": "user", "content": rec.get("content", "")})
            result.user_turns += 1
        elif kind == "feedback_message":
            # A done-gate or reviewer repair request: in history, not a new turn.
            pending, pending_results = None, []
            messages.append({"role": "user", "content": rec.get("content", "")})
        elif kind == "steer_message":
            # An operator message injected mid-turn: in history, not a new turn.
            pending, pending_results = None, []
            messages.append({"role": "user", "content": rec.get("content", ""), "_steer": True})
        elif kind == "assistant_message":
            msg: dict[str, Any] = {"role": "assistant", "content": rec.get("content", "")}
            calls = rec.get("tool_calls") or []
            if calls:
                msg["tool_calls"] = calls
                saw_tool_call = True
                pending, pending_results = msg, []
            else:
                pending, pending_results = None, []
            messages.append(msg)
        elif kind == "tool_result":
            saw_tool_result = True
            messages.append({
                "role": "tool",
                "tool_call_id": rec.get("tool_call_id"),
                "name": rec.get("name"),
                "content": rec.get("content", ""),
            })
            if pending is not None:
                pending_results.append(rec)
                _pair_call_ids(pending, pending_results)
        elif kind == "sensitive_context":
            result.sensitive = True
        elif kind == "stuck_nudge":
            if messages[-1].get("role") == "tool":
                messages[-1]["content"] += "\n\n" + (rec.get("text") or stuck.nudge_text)
        elif kind == "context_elide":
            elide_superseded_reads(messages)
        elif kind == "compaction":
            compacted = _apply_compaction(messages, rec, warnings, anchor)
            if compacted is not messages:
                anchor = 1 if rec.get("mid_turn") else None
            messages = result.messages = compacted
            pending, pending_results = None, []

    if saw_tool_call and not saw_tool_result:
        result.complete = False
        warnings.append(
            "transcript predates tool-result logging: tool calls have "
            "no answering results; this session cannot be resumed faithfully"
        )
    result.messages = messages
    return result
