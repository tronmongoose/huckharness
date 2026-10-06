"""Per-step machinery for Session.run_turn.

Exports: MAX_REPAIR_ROUNDS, TurnState, drain_steer, call_model, absorb_reply,
handle_truncated, finish_or_repair, dispatch_step, check_stuck, run_repairs,
finish_turn, finish_halted. Session owns routing and the outer step loop;
this module holds the work inside one step so run_turn reads on one screen.
Every function takes the session first and uses its private surface
(_messages, _emit, _log, _remaining_s and friends). Backends are called
through their module attributes so tests that patch ``ollama.chat`` still
reach them.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coding_harness.context import recall
from coding_harness.core import (
    context_budget,
    cost,
    done_gate,
    recovery,
    stuck,
    turn_guards,
    verify,
)
from coding_harness.models import anthropic, claude_cli, ollama
from coding_harness.security import audit
from coding_harness.security.bash_snapshot import extract_targets

if TYPE_CHECKING:
    from coding_harness.core.router import RouteDecision
    from coding_harness.core.session import Session, SessionResult

MAX_REPAIR_ROUNDS = 3  # in-loop verify-repair cap per prompt (aider reference)


@dataclass
class TurnState:
    """Accumulators for one run_turn invocation, threaded through the step loop."""

    user_prompt: str
    selected_model: str
    route_reason: str
    decision: RouteDecision | None
    tools: list[dict[str, Any]]
    gate: done_gate.Gate
    cwd: str
    tokens_in: int = 0
    tokens_out: int = 0
    thinking_tokens: int | None = None  # stays None unless a model exposes it
    tool_call_count: int = 0
    repair_rounds: int = 0  # in-loop verify-repair, capped at MAX_REPAIR_ROUNDS
    review_rounds: int = 0  # agentic-review repairs, capped at MAX_REVIEW_ROUNDS
    last_verify_report: str | None = None  # the newest failing gate report, for the reviewer
    all_edited: set[str] = field(default_factory=set)  # every file Edit/Write touched this prompt
    bash_ran: bool = False  # a Bash step can change files the edit tracker never sees
    edit_count: int = 0  # successful Edit/Write/Bash steps; the gate verdict is stale once it moves
    tracker: stuck.StepTracker = field(default_factory=stuck.StepTracker)
    num_predict: int | None = None  # raised after a truncated reply, for the rest of the prompt
    deltas_seen: bool = False
    use_cli: bool = False
    last_edit_msg: dict[str, Any] | None = None
    recall_block: str | None = None  # built once per turn by context.recall on the first local step

    @property
    def is_frontier(self) -> bool:
        """The router sent this turn to a frontier model on complexity."""
        return (
            self.decision is not None
            and self.decision.route == "frontier"
            and self.route_reason == "complexity_high"
        )


def _stream_kw(session: Session, state: TurnState) -> dict[str, Any]:
    """``on_delta`` only when a sink listens, so non-sink calls keep the pre-streaming signature."""
    if session.event_sink is None:
        return {}

    def _on_delta(text: str) -> None:
        # Cooperative cancel point + live token fanout. Raising here unwinds
        # the ollama read loop up to run_turn's _Interrupted catch.
        state.deltas_seen = True
        session._check_interrupt()
        session._emit_ephemeral("assistant_delta", {
            "turn": session._total_turns, "text": text,
        })

    return {"on_delta": _on_delta}


def drain_steer(session: Session, step: int) -> None:
    """Append queued operator steer messages as user turns and log them for replay.

    Flagged ``_steer`` so rewind does not count them as user turns; the
    ollama wire strips underscore keys. Logged as ``steer_message``, which
    replay rebuilds without bumping ``user_turns``.
    """
    for text in session.take_steers():
        session._messages.append({"role": "user", "content": text, "_steer": True})
        session._log("steer_message", {"turn": session._total_turns, "step": step, "content": text})
        session._emit_ephemeral("steer_injected", {
            "turn": session._user_turn, "step": step, "text": text,
        })


def call_model(session: Session, state: TurnState, step: int) -> dict[str, Any]:
    """One model read on the turn's backend; local turns shrink the prompt first."""
    drain_steer(session, step)
    is_frontier = state.is_frontier
    # Operator-pinned text-only turn rides the cli path too.
    state.use_cli = state.route_reason == "override_turn_cli" or (
        is_frontier and session.frontier_backend == "claude-cli"
    )
    stream_kw = _stream_kw(session, state)
    brain = session.registry.tools.get("Brain")
    if brain is not None:
        brain.local = not (state.use_cli or is_frontier)
    explore = session.registry.tools.get("Explore")
    if explore is not None:
        explore.emit = session._emit_ephemeral
    if not (state.use_cli or is_frontier):
        session._shrink_context(state.selected_model, state.tools)
    # A model read can queue behind other loaded models for minutes; say so,
    # so a UI can show what it waits on.
    session._emit_ephemeral("model_call_start", {
        "turn": session._user_turn,
        "step": step,
        "model": state.selected_model,
        "backend": "claude-cli" if state.use_cli else (
            "anthropic" if is_frontier else "ollama"),
    })
    local = not (state.use_cli or is_frontier)
    messages = recall.view(session, state) if local else session._messages
    common = {"model": state.selected_model, "messages": messages, "tools": state.tools}
    if state.use_cli:
        return claude_cli.chat(decision=state.decision, **common, **stream_kw)
    if is_frontier:
        return anthropic.chat(decision=state.decision, **common)
    with session._retry_scope():
        return ollama.chat(
            **common,
            **session._sampling_kw(state.selected_model, state.num_predict),
            **stream_kw,
            **session._read_timeout_kw(),
        )


def _record_cost(session: Session, state: TurnState, tok_in: int, tok_out: int) -> None:
    """Frontier spend to the shared ledger; claude-cli logs real tokens at $0. Best-effort."""
    if state.route_reason not in ("complexity_high", "override_turn_cli"):
        return
    usage = {"input_tokens": tok_in, "output_tokens": tok_out}
    model = state.selected_model
    try:
        if state.use_cli:
            cost.record(f"claude-cli:{model}", usage, 0.0)
        else:
            cost.record(model, usage, cost.cost_for(model, tok_in, tok_out))
    except Exception as e:  # noqa: BLE001
        session._log("cost_record_error", {"error": f"{type(e).__name__}: {e}"})


def absorb_reply(session: Session, state: TurnState, msg: dict[str, Any]) -> dict[str, Any]:
    """Harvest telemetry, strip ``_usage``, append and log the reply; returns the usage."""
    # _usage is our internal key: it must not leak back to the model.
    usage = msg.pop("_usage", None) or {}
    tok_in = int(usage.get("tokens_in") or 0)
    tok_out = int(usage.get("tokens_out") or 0)
    session.last_prompt_tokens = tok_in
    state.tokens_in += tok_in
    state.tokens_out += tok_out
    t = usage.get("thinking_tokens")
    if t is not None:
        state.thinking_tokens = (state.thinking_tokens or 0) + int(t)
    _record_cost(session, state, tok_in, tok_out)
    session._messages.append(msg)
    session._log("assistant_message", {
        "turn": session._total_turns,
        "content": msg.get("content", ""),
        "tool_calls": msg.get("tool_calls") or [],
    })
    return usage


def handle_truncated(
    session: Session, state: TurnState, msg: dict[str, Any], usage: dict[str, Any],
) -> bool:
    """Answer a max_tokens-cut tool call with errors and raise num_predict; True when it was cut.

    Dispatching an incomplete call would write a half-file. Tool errors, not
    a user turn, keep rewind's user-turn count and the transcript intact.
    """
    if not recovery.is_truncated_call(msg, usage):
        return False
    state.num_predict = recovery.doubled_num_predict(
        state.num_predict or session._profile_for(state.selected_model).num_predict
    )
    turn_guards.reject_truncated_calls(session, msg["tool_calls"])
    session._emit("truncated_response", {
        "turn": session._total_turns, "num_predict": state.num_predict,
    })
    return True


def finish_or_repair(session: Session, state: TurnState, final: str) -> bool:
    """On a no-tool reply run the done gate then the reviewer; True when a repair round was bought."""
    # The UI renders text only from delta events; a backend that never
    # streamed would leave the reply invisible, so emit it once.
    if final and not state.deltas_seen and session.event_sink is not None:
        session._emit_ephemeral("assistant_delta", {
            "turn": session._total_turns, "text": final,
        })
    if session._steer:
        return True  # a steer arrived during the final read; the next step answers it
    edited = bool(state.all_edited) or state.bash_ran
    if (turn_guards.gate_allowed(session, state.gate, edited)
            and turn_guards.run_done_gate(session, state.gate, state.cwd, state.edit_count)):
        return True
    if turn_guards.review_allowed(session, state.all_edited, state.review_rounds) and turn_guards.run_review(
        session, state.user_prompt, state.all_edited,
        state.last_verify_report, state.review_rounds + 1,
    ):
        state.review_rounds += 1
        return True
    return False


def _track_mutation(  # LONG-FN: the Edit/Write and Bash branches share the counters
    session: Session, state: TurnState, tool_name: str, tool_args: dict[str, Any],
    result: Any, tool_msg: dict[str, Any], edited_paths: list[str],
) -> bool:
    """Record what one dispatched call changed; True when the world changed."""
    if tool_name == "Brain" and result.metadata.get("max_tier", 0) >= 2:
        session.mark_sensitive("brain_tool", int(result.metadata["max_tier"]))
    if tool_name == "Bash":
        state.bash_ran = True
    # A review-gate deny is not an error but left the bytes untouched.
    denied = result.metadata.get("denied_by_operator", False)
    if result.is_error or denied or tool_name not in ("Edit", "Write", "Bash"):
        return False
    state.edit_count += 1
    if tool_name == "Bash":
        # A heredoc/redirect/sed -i writes a file without touching Write/Edit,
        # so it would skip the verify gate. extract_targets only returns files
        # that exist, which post-execution is exactly the set the command wrote.
        touched = [str(p) for p in extract_targets(tool_args.get("command", "") or "")]
    else:
        one = result.metadata.get("file") or tool_args.get("file_path")
        touched = [str(one)] if one else []
    for path in touched:
        edited_paths.append(path)
        state.all_edited.add(path)
        state.last_edit_msg = tool_msg
    return True


def _dispatch_one(
    session: Session, state: TurnState, call_id: str, tool_name: str, tool_args: dict[str, Any],
) -> tuple[dict[str, Any], Any]:
    """Baseline if needed, dispatch one parsed call and append its capped result."""
    gate = state.gate
    if gate.cmds and not gate.baselined and done_gate.may_mutate(tool_name, tool_args, state.cwd):
        turn_guards.take_baseline(session, gate, state.cwd)
    result = session.registry.dispatch(tool_name, tool_args)
    tool_msg = {
        "role": "tool",
        "tool_call_id": call_id,
        "name": tool_name,
        "content": context_budget.clamp_tool_result(tool_name, result.content),
    }
    session._messages.append(tool_msg)
    session._log_tool_result(
        tool_msg, is_error=result.is_error, error_class=result.metadata.get("error_class"),
    )
    return tool_msg, result


def dispatch_step(  # LONG-FN: one pass over a step's calls; splitting hides the per-call ordering
    session: Session, state: TurnState, tool_calls: list[dict[str, Any]],
) -> tuple[list[tuple[str, str]], list[bool], bool, dict[str, Any] | None, list[str]]:
    """Dispatch every tool call of one assistant step, one tool message per call.

    Returns (step_calls, step_errors, step_mutated, last_tool_msg,
    edited_paths); ``state.last_edit_msg`` carries the edit that repairs ride.
    """
    edited_paths: list[str] = []
    step_calls: list[tuple[str, str]] = []  # (name, canonical key) per call
    step_errors: list[bool] = []
    step_mutated = False  # a successful Edit/Write/Bash: the world changed
    last_tool_msg: dict[str, Any] | None = None
    state.last_edit_msg = None
    for call in tool_calls:
        session._check_interrupt()
        call_id = session._call_id(call)
        fn = call.get("function") or {}
        tool_name = fn.get("name", "")
        raw_args = fn.get("arguments", "{}")
        tool_args, parse_error = recovery.parse_tool_args(raw_args)
        if parse_error is not None:
            turn_guards.reject_invalid_args(session, call_id, tool_name, raw_args, parse_error)
            step_calls.append((tool_name, stuck.canonical(tool_name, {"_raw": raw_args})))
            step_errors.append(True)
            last_tool_msg = session._messages[-1]
            continue
        step_calls.append((tool_name, stuck.canonical(tool_name, tool_args)))
        denial_msg = turn_guards.paste_echo_denial(
            session, state.user_prompt, call_id, tool_name, tool_args,
        )
        if denial_msg is not None:
            step_errors.append(True)
            last_tool_msg = denial_msg
            continue
        tool_msg, result = _dispatch_one(session, state, call_id, tool_name, tool_args)
        step_errors.append(result.is_error)
        last_tool_msg = tool_msg
        if _track_mutation(session, state, tool_name, tool_args, result, tool_msg, edited_paths):
            step_mutated = True
    return step_calls, step_errors, step_mutated, last_tool_msg, edited_paths


def check_stuck(
    session: Session, state: TurnState, step_calls: list[tuple[str, str]],
    step_errors: list[bool], step_mutated: bool, last_tool_msg: dict[str, Any] | None,
) -> bool:
    """Observe the step for repeats; nudge on the last tool result, True when the turn must halt.

    The nudge rides the last tool result, like a verify-repair report, so
    rewind's user-turn count holds.
    """
    tracker = state.tracker
    verdict = tracker.observe(step_calls, step_errors, step_mutated)
    if verdict == "halt":
        session._emit("stuck", {
            "turn": session._total_turns, "run": tracker.run, "calls": step_calls,
        })
        return True
    if verdict == "nudge" and last_tool_msg is not None:
        last_tool_msg["content"] += "\n\n" + stuck.nudge_text
        # The tool_result record is already written, so the transcript carries
        # the nudge as its own record and replay re-attaches it.
        session._emit("stuck_nudge", {
            "turn": session._total_turns, "run": tracker.run,
            "calls": step_calls, "text": stuck.nudge_text,
        })
    return False


def _verify_repair(session: Session, state: TurnState, edited_paths: list[str]) -> str | None:
    """Lint/compile the step's edits; a failing report rides the edit's tool result."""
    notes: list[str] = []  # files ruff rewrote: not failures, but stale in the model's view
    report: str | None
    try:
        report = verify.verify_files(edited_paths, notes=notes)
    except Exception as e:  # noqa: BLE001
        report = None
        session._log("verify_repair_error", {"error": f"{type(e).__name__}: {e}"})
    assert state.last_edit_msg is not None  # set whenever edited_paths is non-empty
    if notes:
        state.last_edit_msg["content"] += "\n\n" + "\n".join(notes)
    if report:
        state.repair_rounds += 1
        state.last_verify_report = report
        session._emit("verify_repair", {
            "turn": session._total_turns, "round": state.repair_rounds, "files": edited_paths,
        })
        session._log("verify_repair", {"round": state.repair_rounds, "files": edited_paths})
        # Appended to the edit's tool result, not a user message, so rewind's
        # user-turn count (a prompt + its repairs is one turn) stays correct.
        state.last_edit_msg["content"] += "\n\n" + report
    return report


def run_repairs(session: Session, state: TurnState, edited_paths: list[str]) -> None:
    """In-loop verify-repair, then targeted tests when the edit passed; one shared round cap."""
    report = None
    if session.verify_repair and edited_paths and state.repair_rounds < MAX_REPAIR_ROUNDS:
        report = _verify_repair(session, state, edited_paths)
    if not (session.targeted_tests_enabled and edited_paths and report is None
            and state.repair_rounds < MAX_REPAIR_ROUNDS):
        return
    tests_report = done_gate.targeted_report(edited_paths, state.cwd, session._remaining_s())
    if tests_report:
        assert state.last_edit_msg is not None
        state.repair_rounds += 1
        session._emit("targeted_tests", {
            "turn": session._total_turns, "round": state.repair_rounds, "files": edited_paths,
        })
        state.last_edit_msg["content"] += "\n\n" + tests_report


def _append_turn_audit(session: Session, state: TurnState, halted: str, steps: int) -> None:
    """Flush the turn's telemetry row to the audit chain; best-effort."""
    try:
        audit.append_turn(
            session_id=session.session_id,
            user_turn=session._user_turn,
            model=state.selected_model,
            route_reason=state.route_reason,
            tokens_in=state.tokens_in,
            tokens_out=state.tokens_out,
            thinking_tokens=state.thinking_tokens,
            tool_call_count=state.tool_call_count,
            inner_steps=steps,
            halted_reason=halted,
            agent_identity=session.agent_identity,
        )
    except Exception as e:  # noqa: BLE001
        # A broken audit chain must never mask the actual session result.
        session._log("turn_audit_error", {"error": f"{type(e).__name__}: {e}"})


def finish_turn(
    session: Session, state: TurnState, final_text: str, turns: int,
    halted: str, error: str | None = None,
) -> SessionResult:
    """Audit the turn, emit ``turn_done`` and build the SessionResult."""
    from coding_harness.core.session import SessionResult

    _append_turn_audit(session, state, halted, turns)
    gate = state.gate
    passed, report = gate.passed, gate.report
    stale = state.edit_count - gate.edits_at_run
    if passed is not None and stale > 0:
        passed = None
        report = f"{report}\n{stale} edits after the last gate run".strip()
    files = sorted(state.all_edited)
    session.close_steer()  # before turn_done, so a client sees the drop inside the turn
    session._end_turn(files)
    session._emit("turn_done", {
        "turn": session._user_turn,
        "halted_reason": halted,
        "turns": turns,
        "tokens_in": state.tokens_in,
        "tokens_out": state.tokens_out,
        "files_changed": files,
        # The last read's prompt size against the window, for a context meter.
        "prompt_tokens": session.last_prompt_tokens,
        "num_ctx": session.profile.num_ctx,
    })
    return SessionResult(
        final_text=final_text, turns=turns, session_id=session.session_id,
        session_log_path=session._session_log_path, halted_reason=halted, error=error,
        tokens_in=state.tokens_in, tokens_out=state.tokens_out, files_changed=files,
        checks_passed=passed, checks_report=report,
    )


def finish_halted(session: Session, state: TurnState, reason: str, steps: int) -> SessionResult:
    """Result for a turn unwound by the deadline or an operator interrupt."""
    if reason == "deadline":
        session._emit("deadline", {
            "turn": session._user_turn, "at_step": steps,
            "files_changed": sorted(state.all_edited),
        })
        changed = ", ".join(sorted(state.all_edited)) or "none"
        return finish_turn(
            session, state,
            f"(halted: deadline reached after {steps} steps; files changed: {changed})",
            steps, "deadline",
        )
    session._emit("interrupted", {"turn": session._user_turn, "at_step": steps})
    return finish_turn(session, state, "(halted: interrupted by operator)", steps, "interrupted")
