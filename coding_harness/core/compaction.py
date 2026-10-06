"""Auto context compaction for long sessions.

Exports: estimate_chars, find_cut_index, find_step_cut_index,
render_transcript, summarize, compact, compact_steps, CompactionResult. When a session's message history grows past a
character budget, the middle of the conversation (everything between the
system prompt and the last few user turns) is replaced with a single
assistant-role summary produced by the LOCAL model. The summarizer is
always Ollama regardless of the session's routing — history may contain
sensitive content, and compaction must never widen where it travels.

``compact`` runs between user turns and cuts at a user boundary.
``compact_steps`` runs inside one turn, where there may be a single user
message, so it cuts at an agent-step boundary and keeps the turn's task
message verbatim.

Every path fails open: a summarizer error leaves the message list
untouched and the turn proceeds uncompacted.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from coding_harness.models import ollama

# Fits well inside mistral-small3.2's 32k-token context at ~4 chars/token,
# with headroom for the system prompt, tools schema, and the reply.
DEFAULT_THRESHOLD_CHARS = 100_000
DEFAULT_KEEP_RECENT_USERS = 2
SUMMARY_MAX_TOKENS = 1024
# A tool result can be enormous (file reads, command output). The summary
# only needs the gist, so each message body is clamped before rendering.
_RENDER_CLAMP = 2_000

SUMMARY_MARKER = "[Context summary — earlier turns compacted]"

# Working Pattern #4: local models fabricate without an explicit
# input-is-the-only-source constraint.
_SUMMARIZER_SYSTEM = (
    "You are a conversation compactor. The transcript below is the ONLY "
    "valid source of information. Do not add anything from prior or "
    "training knowledge. Summarize what was asked, what was done (files "
    "read/written/edited, commands run and their outcomes), decisions "
    "made, and any unresolved errors or open items. Be specific about "
    "file paths and names that appear in the transcript. Plain prose, "
    "no preamble, under 400 words."
)


@dataclass
class CompactionResult:
    messages: list[dict[str, Any]]
    compacted: bool
    reason: str
    messages_before: int
    messages_after: int
    chars_before: int
    chars_after: int
    summary: str = ""


def estimate_chars(messages: list[dict[str, Any]]) -> int:
    """Serialized size of the history — the closest cheap proxy for what
    actually goes over the wire to the model."""
    return sum(len(json.dumps(m, ensure_ascii=False, default=str)) for m in messages)


def find_cut_index(
    messages: list[dict[str, Any]], keep_recent_users: int,
) -> int | None:
    """Index of the user message that starts the keep-verbatim tail.

    Cutting at a user-message boundary guarantees the tail never opens with
    an orphaned tool result (tool messages must follow their tool_calls).
    Returns None when there is nothing before the tail worth compacting.
    """
    user_indices = [
        i for i, m in enumerate(messages) if m.get("role") == "user"
    ]
    if len(user_indices) <= keep_recent_users:
        return None
    cut = user_indices[-keep_recent_users]
    # Nothing between the system prompt and the tail ⇒ no middle to fold.
    if cut <= 1:
        return None
    return cut


def find_step_cut_index(
    messages: list[dict[str, Any]],
    keep_recent_steps: int = 1,
    anchor: int | None = None,
) -> int | None:
    """Index of the assistant message that starts the keep-verbatim tail.

    Mid-turn compaction cannot cut at a user boundary, so it cuts where an
    agent step starts. A tail that opens with an assistant message never opens
    with an orphaned tool result. ``anchor`` is the turn's task message (the
    first user message when omitted); only steps after it are candidates.
    Returns None when there is no middle worth folding.
    """
    if anchor is None:
        anchor = _first_user(messages)
    if anchor is None or keep_recent_steps < 1:
        return None
    assistants = [
        i for i, m in enumerate(messages)
        if m.get("role") == "assistant" and i > anchor
    ]
    if len(assistants) <= keep_recent_steps:
        return None
    cut = assistants[-keep_recent_steps]
    if cut <= anchor + 1:
        return None
    return cut


def _first_user(messages: list[dict[str, Any]]) -> int | None:
    """Index of the first user message, or None."""
    return next(
        (i for i, m in enumerate(messages) if m.get("role") == "user"), None,
    )


def _only_summary(middle: list[dict[str, Any]]) -> bool:
    """True when the foldable middle is a lone earlier summary; re-summarizing it buys nothing."""
    return (
        len(middle) == 1
        and str(middle[0].get("content") or "").startswith(SUMMARY_MARKER)
    )


def render_transcript(messages: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for m in messages:
        role = m.get("role", "?")
        content = m.get("content") or ""
        if isinstance(content, (list, dict)):
            content = json.dumps(content, ensure_ascii=False, default=str)
        if len(content) > _RENDER_CLAMP:
            content = content[:_RENDER_CLAMP] + " …[truncated]"
        if m.get("tool_calls"):
            calls = ", ".join(
                f"{c.get('function', {}).get('name', '?')}"
                f"({(c.get('function', {}).get('arguments') or '')[:200]})"
                for c in m["tool_calls"]
            )
            lines.append(f"{role} → tool calls: {calls}")
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def summarize(
    middle: list[dict[str, Any]],
    *,
    model: str,
    chat_fn: Callable[..., dict[str, Any]] | None = None,
) -> str:
    """Fold the middle of the conversation into one summary string.

    Raises on model/transport failure — the caller decides fail-open.
    """
    chat = chat_fn or ollama.chat
    msg = chat(
        model=model,
        messages=[
            {"role": "system", "content": _SUMMARIZER_SYSTEM},
            {"role": "user", "content": render_transcript(middle)},
        ],
        tools=None,
        max_tokens=SUMMARY_MAX_TOKENS,
    )
    summary = (msg.get("content") or "").strip()
    if not summary:
        raise ValueError("summarizer returned empty content")
    return summary


def compact(
    messages: list[dict[str, Any]],
    *,
    model: str,
    threshold_chars: int = DEFAULT_THRESHOLD_CHARS,
    keep_recent_users: int = DEFAULT_KEEP_RECENT_USERS,
    chat_fn: Callable[..., dict[str, Any]] | None = None,
) -> CompactionResult:
    """Compact ``messages`` if it exceeds ``threshold_chars``.

    Returns a NEW list when compaction happens; the input list is never
    mutated. Shape after compaction:
        [system prompt, assistant summary, <tail from last N user turns>]
    Fail-open: any summarizer error returns the original list with
    ``compacted=False`` and the error in ``reason``.
    """
    chars_before = estimate_chars(messages)
    n_before = len(messages)

    def _skip(reason: str) -> CompactionResult:
        return CompactionResult(
            messages=messages, compacted=False, reason=reason,
            messages_before=n_before, messages_after=n_before,
            chars_before=chars_before, chars_after=chars_before,
        )

    if chars_before <= threshold_chars:
        return _skip("under_threshold")
    if not messages or messages[0].get("role") != "system":
        return _skip("no_system_prompt")
    cut = find_cut_index(messages, keep_recent_users)
    if cut is None:
        return _skip("too_few_turns")

    middle = messages[1:cut]
    try:
        summary = summarize(middle, model=model, chat_fn=chat_fn)
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        return _skip(f"summarizer_error: {type(e).__name__}: {e}")

    new_messages = [
        messages[0],
        {"role": "assistant", "content": f"{SUMMARY_MARKER}\n{summary}"},
        *messages[cut:],
    ]
    return CompactionResult(
        messages=new_messages,
        compacted=True,
        reason="over_threshold",
        messages_before=n_before,
        messages_after=len(new_messages),
        chars_before=chars_before,
        chars_after=estimate_chars(new_messages),
        summary=summary,
    )


def compact_steps(  # LONG-FN: mirrors compact(); the skip/result plumbing is the shared shape
    messages: list[dict[str, Any]],
    *,
    model: str,
    anchor: int | None = None,
    keep_recent_steps: int = 1,
    chat_fn: Callable[..., dict[str, Any]] | None = None,
) -> CompactionResult:
    """Fold a turn's earlier steps (and any older history) into one summary.

    Shape after: [system, task message, assistant summary, <tail from the last
    ``keep_recent_steps`` agent steps>]. The task message at ``anchor`` (first
    user message when omitted) rides verbatim so the model keeps its goal.
    Never mutates the input. Fail-open like ``compact``.
    """
    chars_before = estimate_chars(messages)
    n_before = len(messages)

    def _skip(reason: str) -> CompactionResult:
        return CompactionResult(
            messages=messages, compacted=False, reason=reason,
            messages_before=n_before, messages_after=n_before,
            chars_before=chars_before, chars_after=chars_before,
        )

    if not messages or messages[0].get("role") != "system":
        return _skip("no_system_prompt")
    if anchor is None:
        anchor = _first_user(messages)
    if anchor is None or messages[anchor].get("role") != "user":
        return _skip("no_user_message")
    cut = find_step_cut_index(messages, keep_recent_steps, anchor)
    if cut is None:
        return _skip("too_few_steps")
    middle = messages[1:anchor] + messages[anchor + 1:cut]
    if not middle or _only_summary(middle):
        return _skip("too_few_steps")
    try:
        summary = summarize(middle, model=model, chat_fn=chat_fn)
    except Exception as e:  # noqa: BLE001 — fail-open by contract
        return _skip(f"summarizer_error: {type(e).__name__}: {e}")

    new_messages = [
        messages[0],
        messages[anchor],
        {"role": "assistant", "content": f"{SUMMARY_MARKER}\n{summary}"},
        *messages[cut:],
    ]
    return CompactionResult(
        messages=new_messages,
        compacted=True,
        reason="over_budget",
        messages_before=n_before,
        messages_after=len(new_messages),
        chars_before=chars_before,
        chars_after=estimate_chars(new_messages),
        summary=summary,
    )
