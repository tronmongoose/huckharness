"""Memory proposals: after a local turn worth learning from, a local model drafts memories.

Exports ``enabled``, ``signals``, ``render_turn``, ``parse_candidates``,
``generator_model``, ``generate`` and ``after_turn``. Storage and the
operator's decision live in ``core.memory_store``.

A turn qualifies when its history holds an operator correction (a user
message after an assistant message inside the turn, or a follow-up prompt
that opens with a correction cue), a permission or gate
denial, a failed check the turn then fixed, or a Brain or recall hit. The
generator is the local reviewer model (``core.review``), never a frontier
backend, and runs on a daemon thread so ``turn_done`` is not delayed. Its
reply is untrusted: ``parse_candidates`` rejects the whole reply on any
malformed item. Nothing reaches the memory directory without an operator
decision. Kill switch: HARNESS_MEMORY_PROPOSALS=0.

A sensitive session, or a turn the router flagged sensitive, drafts
nothing. An approved memory's description lands in MEMORY.md, which every
later system prompt carries, frontier sessions included; confidential
material must not ride that path. A session that is not sensitive has seen
nothing above internal (a tier-2 hit marks it), so every proposal is
frontier-safe by construction and needs no per-proposal tier.
"""
from __future__ import annotations

import json
import os
import re
import threading
from typing import TYPE_CHECKING, Any

from coding_harness.core import memory_store, review
from coding_harness.models import ollama

if TYPE_CHECKING:
    from coding_harness.core.session import Session, SessionResult
    from coding_harness.core.turn_loop import TurnState

MAX_CANDIDATES = 3
TOOL_CHARS = 500
TRANSCRIPT_CHARS = 16_000
TIMEOUT_S = 60.0
_DENIED = ("BLOCKED by", "denied by operator", "DENIED")
_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+){0,11}$")
_KEYS = frozenset({"name", "description", "type", "body"})
_CUE = re.compile(r"^\W*(?:no|not|wrong|instead|actually|rather)\b|^\W*use\b.*\bnot\b", re.I)
_SYSTEM = "You distil durable lessons from a coding session. Answer with ONLY JSON."
_INSTRUCTIONS = (
    "From the session turn above, propose 0 to 3 memories worth keeping for future "
    "sessions in this repository: an operator correction, a rule learned from a "
    "denial, a fix for a failed check, or a fact the notes supplied. Propose "
    "nothing when the turn taught nothing durable.\n\n"
    'Answer with ONLY this JSON object: {"memories": [{"name": "<kebab-case slug>", '
    '"description": "<one line>", "type": "feedback" | "project" | "reference" | "user", '
    '"body": "<markdown: the rule or fact, then a line starting **Why:** and a line '
    'starting **How to apply:**>"}]}'
)


def enabled() -> bool:
    """Proposals run unless HARNESS_MEMORY_PROPOSALS=0."""
    return os.environ.get("HARNESS_MEMORY_PROPOSALS", "1") != "0"


def _turn_messages(session: Session) -> list[dict[str, Any]]:
    """This turn's messages, from its prompt on."""
    anchor = session._turn_anchor
    return list(session._messages[anchor:] if anchor is not None else session._messages[-1:])


def is_correction_prompt(prompt: str, prior_turn: bool) -> bool:
    """A follow-up prompt that opens by correcting the previous turn."""
    return prior_turn and bool(_CUE.search(prompt or ""))


def signals(
    messages: list[dict[str, Any]], state: TurnState, result: SessionResult,
    prior_turn: bool = False,
) -> list[str]:
    """Why this turn is worth a proposal pass; empty when it is not."""
    found: list[str] = []
    roles = [m.get("role") for m in messages]
    if any(r == "user" and roles[i - 1] == "assistant" for i, r in enumerate(roles) if i) or (
            is_correction_prompt(state.user_prompt, prior_turn)):
        found.append("correction")
    if any(m.get("role") == "tool" and str(m.get("content", "")).startswith(_DENIED)
           for m in messages):
        found.append("denial")
    if (state.repair_rounds or state.review_rounds) and result.halted_reason == "model_done":
        found.append("fixed_check")
    if (state.recall_block and "## Recalled notes" in state.recall_block) or any(
            m.get("role") == "tool" and m.get("name") == "Brain" for m in messages):
        found.append("brain")
    return found


def render_turn(messages: list[dict[str, Any]]) -> str:
    """The turn as plain text for the generator, tool output cut to ``TOOL_CHARS``."""
    lines = []
    for m in messages:
        content = str(m.get("content") or "")
        if m.get("role") == "tool":
            content = content[:TOOL_CHARS] + ("..." if len(content) > TOOL_CHARS else "")
            lines.append(f"[tool {m.get('name', '')}]\n{content}")
            continue
        calls = [c.get("function", {}).get("name", "") for c in m.get("tool_calls") or []]
        suffix = f"\n(calls: {', '.join(calls)})" if calls else ""
        lines.append(f"[{m.get('role')}]\n{content}{suffix}")
    return "\n\n".join(lines)[-TRANSCRIPT_CHARS:]


def _json_payload(text: str) -> Any:
    """The JSON value in a reply, after any leaked thinking; ValueError when there is none."""
    text = text.rsplit("</think>", 1)[-1].strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    return json.loads(fenced.group(1) if fenced else text)


def _check(item: Any) -> dict[str, str]:
    """One validated candidate; ValueError names the first defect."""
    if not isinstance(item, dict) or set(item) != _KEYS:
        raise ValueError("each memory must carry exactly name, description, type, body")
    if not all(isinstance(item[k], str) for k in _KEYS):
        raise ValueError("every field must be a string")
    name, desc, kind, body = (item[k].strip() for k in ("name", "description", "type", "body"))
    if not _NAME.match(name) or not 3 <= len(name) <= 60:
        raise ValueError(f"name {name[:60]!r} is not a kebab-case slug of 3-60 chars")
    if "\n" in desc or not 10 <= len(desc) <= 200:
        raise ValueError("description must be one line of 10-200 chars")
    if kind not in memory_store.TYPES:
        raise ValueError(f"type {kind[:20]!r} not in {sorted(memory_store.TYPES)}")
    if "**Why:**" not in body or "**How to apply:**" not in body:
        raise ValueError("body needs a **Why:** and a **How to apply:** line")
    if not memory_store.MIN_BODY <= len(body) <= memory_store.MAX_BODY:
        raise ValueError(f"body must be {memory_store.MIN_BODY}-{memory_store.MAX_BODY} chars")
    return {"name": name, "description": desc, "type": kind, "body": body}


def parse_candidates(text: str) -> list[dict[str, str]]:
    """Strictly validated candidates from a generator reply; any defect rejects the reply."""
    data = _json_payload(text)
    if isinstance(data, dict) and set(data) == {"memories"}:
        data = data["memories"]
    if not isinstance(data, list):
        raise ValueError("reply is not a list of memories")
    if len(data) > MAX_CANDIDATES:
        raise ValueError(f"{len(data)} memories proposed, at most {MAX_CANDIDATES}")
    return [_check(item) for item in data]


def generator_model(fallback: str) -> str:
    """The summarize-role tag; the session's coder when it is not pulled. Never a frontier model."""
    from coding_harness.core.model_roles import role_model

    model = role_model("summarize")
    if not review._tag_present(model):
        model = fallback
    ollama.assert_model_allowed(model)
    return model


def generate(transcript: str, model: str) -> list[dict[str, str]]:
    """Ask the local model for candidates; ValueError on an unusable reply."""
    msg = ollama.chat(
        model=model,
        messages=[{"role": "system", "content": _SYSTEM},
                  {"role": "user", "content": f"{transcript}\n\n{_INSTRUCTIONS}"}],
        tools=None, max_tokens=1200, temperature=0.1, timeout=TIMEOUT_S,
        response_format={"type": "json_object"},
    )
    return parse_candidates(msg.get("content", "") or "")


def _work(session: Session, transcript: str, turn: int) -> None:
    """Thread body: generate, store each candidate, emit ``memory_proposed``; faults are logged."""
    try:
        for cand in generate(transcript, generator_model(session.model)):
            meta = memory_store.write_proposal(session.session_id, cand, turn)
            session._emit("memory_proposed", meta)
    except Exception as e:  # noqa: BLE001 a failed proposal pass must never touch the session
        session._log("memory_proposal_error", {"turn": turn, "error": f"{type(e).__name__}: {e}"})


def after_turn(session: Session, state: TurnState, result: SessionResult) -> threading.Thread | None:
    """Start a proposal pass for a qualifying local turn; the started thread, else None."""
    frontier = state.is_frontier or state.use_cli or state.route_reason in (
        "complexity_high", "override_turn_cli")
    if not (session.propose_memories and enabled()) or frontier:
        return None
    if session.sensitive_context or getattr(state.decision, "sensitivity_flag", False):
        session._log("memory_proposal_skipped", {"turn": session._user_turn, "reason": "sensitive"})
        return None
    messages = _turn_messages(session)
    if not signals(messages, state, result, prior_turn=session._user_turn > 1):
        return None
    worker = threading.Thread(
        target=_work, args=(session, render_turn(messages), session._user_turn),
        name=f"memory-proposals-{session.session_id}", daemon=True,
    )
    worker.start()
    return worker
