"""Spec-then-run routes for serve: plan, edit, act, autonomy, todo.

A plan turn drops the session to OFF, asks for a markdown plan, restores the
level and saves the final text to ``plan_path_for(session_id)``. The operator
edits that one file over PUT, then ``act`` raises autonomy and runs the plan
with an instruction to keep a TodoWrite checklist. Prompts, the plan path and
the level order are shared with the REPL (``modes.repl_plan``).

Routes (``{id}`` is a live session):
  POST /v1/sessions/{id}/plan      {"goal"} -> 202; plan_saved | plan_failed
  GET  /v1/sessions/{id}/plan      -> {exists, text, mtime}
  PUT  /v1/sessions/{id}/plan      {"text"} -> {bytes}; plan_updated, 409 mid-turn
  POST /v1/sessions/{id}/act       {"autonomy"?} -> 202; 400 on off or no plan
  POST /v1/sessions/{id}/autonomy  {"level"} -> {from, to, mode}; 409 mid-turn
  GET  /v1/sessions/{id}/todo      -> {items}

Exports: GET_ROUTES, POST_ROUTES, PUT_ROUTES, OPENAPI_PATHS, handle(),
apply_autonomy().
"""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from typing import Any

from coding_harness.core.envelope import preset
from coding_harness.core.mode import Autonomy, parse_autonomy, set_autonomy
from coding_harness.modes.repl_plan import (
    _LEVEL_ORDER,
    ACT_INSTRUCTION,
    PLAN_INSTRUCTION,
    plan_path_for,
)
from coding_harness.security import audit
from coding_harness.tools.todo import load_todos

GET_ROUTES = frozenset({"plan", "todo"})
POST_ROUTES = frozenset({"plan", "act", "autonomy"})
PUT_ROUTES = frozenset({"plan"})
MAX_GOAL = 20_000
MAX_PLAN = 200_000
MIN_PLAN = 200
_PLAN_LINE = re.compile(r"^\s*(?:#{1,6}\s|[-*+]\s|\d+[.)]\s)", re.M)
TODO_INSTRUCTION = (
    "Start by calling TodoWrite with the plan's steps; "
    "update statuses as you complete them.\n\n"
)

_ID = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]


def _op(summary: str, code: str, what: str) -> dict[str, Any]:
    """One OpenAPI operation on a session sub-route."""
    return {"summary": summary, "parameters": _ID, "responses": {code: {"description": what}}}


OPENAPI_PATHS: dict[str, Any] = {
    "/v1/sessions/{id}/plan": {
        "get": _op("The saved plan: {exists, text, mtime}", "200", "plan"),
        "put": _op("Replace the plan text: body {text}", "200", "saved"),
        "post": _op("Run a read-only planning turn: body {goal}", "202", "accepted"),
    },
    "/v1/sessions/{id}/act": {
        "post": _op("Run the saved plan: body {autonomy}", "202", "accepted"),
    },
    "/v1/sessions/{id}/autonomy": {
        "post": _op("Change the autonomy level: body {level}", "200", "changed"),
    },
    "/v1/sessions/{id}/todo": {
        "get": _op("The TodoWrite checklist: {items}", "200", "items"),
    },
}


def _swap_preset(entry: Any, level: Autonomy, settings: Any, reason: str) -> None:
    """Rebuild a preset envelope's default grants for ``level``.

    Operator grants (JIT allow_always) survive. A custom or resumed envelope
    is left alone: its scope was chosen explicitly. Audited before it takes
    effect, so an unrecordable change does not happen.
    """
    envelope = entry.envelope
    if envelope is None or not getattr(entry, "envelope_preset", False):
        return
    fresh = preset(os.getcwd(), level, settings).grants
    audit.append_envelope_change(
        session_id=entry.session.session_id,
        action="preset",
        grant={"level": level.value, "grants": len(fresh)},
        reason=reason,
        agent_identity=entry.identity,
    )
    kept = [g for g in envelope.grants if g.granted_by != "default"]
    envelope.grants = fresh + kept


def apply_autonomy(entry: Any, level: Autonomy, reason: str, settings: Any) -> Autonomy:
    """Envelope, level, mode and event in one step; returns the previous level."""
    previous = entry.session.registry.autonomy
    if level is previous:
        return previous
    _swap_preset(entry, level, settings, reason)
    set_autonomy(entry.session, level, reason)
    entry.session._emit("autonomy_change", {
        "from": previous.value, "to": level.value, "reason": reason,
    })
    return previous


def _read_plan(session_id: str) -> dict[str, Any]:
    """{exists, text, mtime} for the session's fixed plan path."""
    path = plan_path_for(session_id)
    try:
        return {"exists": True, "text": path.read_text(encoding="utf-8"),
                "mtime": path.stat().st_mtime}
    except OSError:
        return {"exists": False, "text": "", "mtime": None}


def _write_plan(session_id: str, text: str) -> int:
    """Write the plan file atomically and return its size in bytes."""
    path = plan_path_for(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    tmp = path.with_suffix(".md.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return len(data)


def _turn_busy(entry: Any) -> bool:
    """Whether a turn is running or accepted for this session."""
    with entry.listeners_lock:
        return bool(entry.turn_active)


def _post_plan(h: Any, state: Any, entry: Any, sid: str, body: dict[str, Any]) -> None:
    """Accept a planning turn run at OFF; the level comes back in finally."""
    goal = body.get("goal")
    if not isinstance(goal, str) or not goal.strip() or len(goal) > MAX_GOAL:
        h._send_error_json(400, f"body.goal must be a non-empty string under {MAX_GOAL} chars")
        return
    session = entry.session

    @contextmanager
    def _at_off():
        prior = apply_autonomy(entry, Autonomy.OFF, "serve_plan", state.settings)
        try:
            yield
        finally:
            apply_autonomy(entry, prior, "serve_plan_restore", state.settings)

    def _save(result: Any, error: str | None) -> None:
        # Only a turn the model finished is a plan: a halt text (interrupt,
        # deadline, stuck) must never overwrite the operator's plan.
        reason = _plan_failure(result, error)
        if reason is not None:
            session._emit("plan_failed", {"reason": reason})
            return
        text = result.final_text.strip()
        size = _write_plan(sid, text + "\n")
        session._emit("plan_saved", {"path": str(plan_path_for(sid)), "bytes": size})

    h._start_turn(entry, sid, PLAN_INSTRUCTION + goal.strip(), model_override=None,
                  max_time_s=None, checks=None, scope=_at_off, on_result=_save)


def _plan_failure(result: Any, error: str | None) -> str | None:
    """Why ``result`` is not a usable plan, or None when it is one."""
    if result is None:
        return error or "the planning turn failed"
    halted = getattr(result, "halted_reason", None)
    if halted != "model_done":
        return getattr(result, "error", None) or f"planning halted: {halted}"
    text = (getattr(result, "final_text", None) or "").strip()
    if not text:
        return "the turn produced no plan text"
    if not looks_like_plan(text):
        return "not_a_plan"
    return None


def looks_like_plan(text: str) -> bool:
    """A markdown heading or list item and at least ``MIN_PLAN`` chars; an apology is neither."""
    return len(text) >= MIN_PLAN and _PLAN_LINE.search(text) is not None


def _put_plan(h: Any, entry: Any, sid: str, body: dict[str, Any]) -> None:
    """Overwrite the fixed plan path with the operator's edit."""
    text = body.get("text")
    if not isinstance(text, str) or len(text) > MAX_PLAN:
        h._send_error_json(400, f"body.text must be a string under {MAX_PLAN} chars")
        return
    if _turn_busy(entry):
        h._send_error_json(409, "a turn is running; edit the plan after it ends")
        return
    size = _write_plan(sid, text)
    entry.session._emit("plan_updated", {"bytes": size})
    h._send_json(200, {"session_id": sid, "bytes": size})


def _act_level(body: dict[str, Any], current: Autonomy) -> tuple[Autonomy | None, str | None]:
    """The level to run the plan at: the body's, else at least LOW."""
    raw = body.get("autonomy")
    if raw is None:
        return max(current, Autonomy.LOW, key=_LEVEL_ORDER.index), None
    if not isinstance(raw, str):
        return None, "body.autonomy must be a string"
    try:
        level = parse_autonomy(raw)
    except ValueError as e:
        return None, str(e)
    if level is Autonomy.OFF:
        return None, "act needs autonomy low, medium or high; off cannot edit"
    return level, None


def _post_act(h: Any, state: Any, entry: Any, sid: str, body: dict[str, Any]) -> None:
    """Raise autonomy and run the saved plan with a TodoWrite checklist."""
    level, problem = _act_level(body, entry.session.registry.autonomy)
    if level is None:
        h._send_error_json(400, problem or "invalid autonomy")
        return
    plan = _read_plan(sid)
    if not plan["exists"] or not plan["text"].strip():
        h._send_error_json(400, "no plan for this session; POST .../plan first")
        return
    @contextmanager
    def _raise():
        # Applied in the worker under entry.lock, so a refused (409) act never
        # leaves the level raised and no other turn can run at it. No restore:
        # the operator chose this level for the session.
        apply_autonomy(entry, level, "serve_act", state.settings)
        yield

    h._start_turn(entry, sid, ACT_INSTRUCTION + TODO_INSTRUCTION + plan["text"],
                  model_override=None, max_time_s=None, checks=None, scope=_raise)


def _post_autonomy(h: Any, state: Any, entry: Any, sid: str, body: dict[str, Any]) -> None:
    """Change the level between turns; 409 while one runs or is accepted.

    A mid-turn change would hand a read-only plan turn mutating tools and be
    undone by its restore, so the level only moves while the session is idle.
    The session lock is taken without blocking: a turn accepted a moment ago
    holds it, and that is a 409 too.
    """
    raw = body.get("level")
    if not isinstance(raw, str):
        h._send_error_json(400, "body.level must be a string")
        return
    try:
        level = parse_autonomy(raw)
    except ValueError as e:
        h._send_error_json(400, str(e))
        return
    if _turn_busy(entry) or not entry.lock.acquire(blocking=False):
        h._send_error_json(409, "a turn is running; change autonomy after it ends")
        return
    try:
        previous = apply_autonomy(entry, level, "serve_autonomy", state.settings)
    finally:
        entry.lock.release()
    h._send_json(200, {"session_id": sid, "from": previous.value, "to": level.value,
                       "mode": entry.session.mode.value})


def _get_todo(h: Any, entry: Any, sid: str) -> None:
    """The live list, else the mirror (a resumed session starts empty in memory)."""
    tool = entry.session.registry.tools.get("TodoWrite")
    items = getattr(tool, "items", None) or load_todos(sid)
    h._send_json(200, {"session_id": sid, "items": items})


def handle(h: Any, state: Any, method: str, entry: Any, sid: str, sub: str,
           body: dict[str, Any] | None = None) -> None:
    """Dispatch one route from ``GET_ROUTES``/``POST_ROUTES``/``PUT_ROUTES``."""
    body = body or {}
    if method == "GET" and sub == "plan":
        h._send_json(200, {"session_id": sid, **_read_plan(sid)})
    elif method == "GET" and sub == "todo":
        _get_todo(h, entry, sid)
    elif method == "PUT" and sub == "plan":
        _put_plan(h, entry, sid, body)
    elif method == "POST" and sub == "plan":
        _post_plan(h, state, entry, sid, body)
    elif method == "POST" and sub == "act":
        _post_act(h, state, entry, sid, body)
    elif method == "POST" and sub == "autonomy":
        _post_autonomy(h, state, entry, sid, body)
    else:
        h._send_error_json(405, f"{method} not allowed on {sub}")
