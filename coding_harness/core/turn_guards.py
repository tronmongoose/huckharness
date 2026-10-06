"""Turn-level gates for Session.run_turn.

Exports: MAX_REVIEW_ROUNDS, PASTE_ECHO_DENIAL_TEMPLATE, deadline_reserve_s,
reject_invalid_args, reject_truncated_calls, review_allowed, gate_allowed,
take_baseline, run_done_gate, feed_back, run_review, paste_echo_denial,
review_diffs.
Each function takes the session as its first argument and uses its private
surface (_messages, _emit, _log, _remaining_s). Session re-exports
MAX_REVIEW_ROUNDS so callers keep importing it from core.session.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coding_harness.core import done_gate, recovery, review
from coding_harness.security import audit
from coding_harness.security.paste_echo import is_paste_echo
from coding_harness.tools.edit import _unified_diff

if TYPE_CHECKING:
    from coding_harness.core.session import Session

MAX_REVIEW_ROUNDS = 1  # agentic-review repair cap per prompt
_BASH_REVIEW_CHARS = 3000  # a Bash-written file has no pre-image, so the reviewer sees its head

PASTE_ECHO_DENIAL_TEMPLATE = (
    "DENIED by paste-echo gate ({reason}): the proposed shell command "
    "appeared to echo the user's prompt rather than be authored by you. "
    "If the user intended this command, they will type it themselves."
)


def deadline_reserve_s() -> float:
    """Seconds a turn keeps in hand before running the agentic reviewer (HARNESS_DEADLINE_RESERVE_S)."""
    try:
        return float(os.environ.get("HARNESS_DEADLINE_RESERVE_S", "120"))
    except ValueError:
        return 120.0


def reject_invalid_args(
    session: Session, call_id: str, tool_name: str, raw_args: str, error: str,
) -> None:
    """Feed an unparseable tool call back as a tool error, audited, without dispatching."""
    try:
        entry = recovery.audit_invalid_args(
            session_id=session.session_id, tool=tool_name,
            raw_args=raw_args, agent_identity=session.agent_identity,
        )
        session._emit("audit_entry", entry)
    except Exception as e:  # noqa: BLE001
        session._log("invalid_args_audit_error", {"error": f"{type(e).__name__}: {e}"})
    tool_msg = {
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": recovery.INVALID_ARGS_TEXT.format(error=error),
    }
    session._messages.append(tool_msg)
    session._log_tool_result(tool_msg, is_error=True, error_class="invalid_args")
    session._emit("tool_args_invalid", {
        "turn": session._total_turns, "tool": tool_name, "error": error,
    })


def reject_truncated_calls(session: Session, tool_calls: list[dict[str, Any]]) -> None:
    """Answer every call of a truncated reply with a tool error, undispatched."""
    for call in tool_calls:
        tool_msg = {
            "role": "tool",
            "tool_call_id": session._call_id(call),
            "name": (call.get("function") or {}).get("name", ""),
            "content": recovery.TRUNCATED_TEXT,
        }
        session._messages.append(tool_msg)
        session._log_tool_result(tool_msg, is_error=True, error_class="truncated")


def review_allowed(session: Session, all_edited: set[str], review_rounds: int) -> bool:
    """Whether the agentic reviewer runs now; a thin deadline reserve skips it."""
    if not (session.agentic_review and all_edited
            and review_rounds < MAX_REVIEW_ROUNDS):
        return False
    remaining = session._remaining_s()
    if remaining is not None and remaining < deadline_reserve_s():
        session._emit("review_skipped", {
            "reason": "deadline_reserve",
            "remaining_s": round(remaining, 1),
        })
        return False
    return True


def gate_allowed(session: Session, gate: done_gate.Gate, edited: bool) -> bool:
    """Whether the green-before-done checks run now; no edits or a thin reserve skips them."""
    if not gate.cmds:
        return False
    remaining = session._remaining_s()
    reason = None
    # No baseline means nothing mutating ran, whatever the Bash flag says.
    if not edited or not gate.baselined:
        reason = "no_edits"
    elif remaining is not None and remaining < deadline_reserve_s():
        reason = "deadline_reserve"
    if reason is None:
        return True
    session._emit("checks_skipped", {
        "reason": reason,
        "remaining_s": None if remaining is None else round(remaining, 1),
    })
    return False


def take_baseline(session: Session, gate: done_gate.Gate, cwd: str) -> None:
    """Record pre-edit failures, announcing the run first so a UI can say why it waits."""
    session._emit("checks_baseline_start", {"checks": gate.cmds})
    session._emit("checks_baseline", {
        "failures": done_gate.run_baseline(gate, cwd, session._remaining_s()),
        "checks": gate.cmds,
    })


def run_done_gate(session: Session, gate: done_gate.Gate, cwd: str, edit_count: int) -> bool:
    """Run the green-before-done checks; True when a new failure bought a repair round."""
    gate.edits_at_run = edit_count
    verdict = done_gate.run_gate(gate, cwd, session._remaining_s())
    session._emit("done_gate", {
        "round": gate.rounds + 1,
        "passed": verdict.passed,
        "failures": verdict.failures,
        "skipped_reason": verdict.skipped_reason,
    })
    if verdict.passed is False and gate.rounds < done_gate.MAX_GATE_ROUNDS:
        gate.rounds += 1
        feed_back(session, "done_gate", (
            "The checks below failed. Fix the cause, "
            f"then finish again:\n{verdict.report}"
        ))
        return True
    return False


def feed_back(session: Session, source: str, content: str) -> None:
    """Append a repair request as a user message and log it so replay rebuilds it.

    Logged as ``feedback_message``, not ``user_message``: it is part of the
    same prompt, so a resumed session must not count it as a user turn.
    """
    session._messages.append({"role": "user", "content": content})
    session._log("feedback_message", {
        "turn": session._total_turns, "source": source, "content": content,
    })


def run_review(
    session: Session, user_prompt: str, all_edited: set[str],
    verify_report: str | None, review_round: int,
) -> bool:
    """Grade the prompt's diffs; True when the reviewer's concerns were fed back for repair."""
    if session.sensitive_context and review.backend() == "claude-cli":
        session._emit("review_rerouted", {
            "from": "claude-cli", "to": "local", "reason": "sensitive_context",
        })
    with session._retry_scope(single=True):
        verdict = review.review_change(
            user_prompt, review_diffs(session, all_edited),
            verify_report, fallback_model=session.model,
            sensitive=session.sensitive_context,
        )
    session._emit("agentic_review", {
        "round": review_round,
        "approved": verdict.approved,
        "files": sorted(all_edited),
        "backend": verdict.backend,
        "model": verdict.model,
        "parse_error": verdict.parse_error,
        "fallback_used": verdict.fallback_used,
    })
    if verdict.approved:
        return False
    feed_back(session, "review", (
        "A code reviewer flagged this change: "
        f"{verdict.concerns}\nAddress it, then finish."
    ))
    return True


def paste_echo_denial(  # LONG-FN: detect, ask, audit and feed back one denial in call order
    session: Session, user_prompt: str, call_id: str, tool_name: str,
    tool_args: dict[str, Any],
) -> dict[str, Any] | None:
    """The refusal fed back for a Bash call that echoes the prompt; None when it may dispatch.

    Bash-only: Write/Edit have their own diff-confirm path. Runs before
    Sentinel so an operator denial short-circuits dispatch entirely.
    """
    if tool_name != "Bash":
        return None
    command = tool_args.get("command", "") or ""
    matches, reason = is_paste_echo(user_prompt, command)
    if not matches:
        return None
    session._emit("paste_echo_detected", {
        "tool": tool_name,
        "reason": reason,
        "command_preview": command[:200],
        "user_prompt_preview": user_prompt[:200],
    })
    if session.paste_echo_callback is not None and session.paste_echo_callback(
        user_prompt, tool_name, tool_args,
    ):
        return None
    denial = PASTE_ECHO_DENIAL_TEMPLATE.format(reason=reason)
    try:
        entry = audit.append(
            session_id=session.session_id,
            tool=tool_name,
            args=tool_args,
            result=denial,
            allowed=False,
            sentinel_reason="paste_echo_denied",
            sentinel_path="paste_echo",
            denied_by_operator=True,
            agent_identity=session.agent_identity,
        )
        session._emit("audit_entry", entry)
    except Exception as e:  # noqa: BLE001
        session._log("paste_echo_audit_error", {"error": f"{type(e).__name__}: {e}"})
    denial_msg = {
        "role": "tool", "tool_call_id": call_id, "name": tool_name, "content": denial,
    }
    session._messages.append(denial_msg)
    session._log_tool_result(denial_msg, is_error=True)
    return denial_msg


def review_diffs(session: Session, all_edited: set[str]) -> dict[str, str]:
    """One unified diff per edited file from its first pre-image this prompt; Bash-written files ride whole."""
    diffs: dict[str, str] = {}
    for path in sorted(all_edited):
        if not Path(path).is_file():
            continue
        current = Path(path).read_text(encoding="utf-8", errors="replace")
        if path in session._first_pre_image:
            before = (session._first_pre_image[path] or b"").decode("utf-8", errors="replace")
            diffs[path] = _unified_diff(before, current, path)
        else:
            diffs[path] = (
                f"--- {path} (unsnapshotted, written by Bash) ---\n"
                + current[:_BASH_REVIEW_CHARS]
            )
    return diffs
