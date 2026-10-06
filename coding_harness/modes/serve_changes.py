"""Serve routes for the GUI changes view: diff, rewind, single-file revert, review gate.

Exports ``review_gate``, ``get_diff``, ``post_rewind``, ``post_revert_file``,
``post_review``, ``POST_ROUTES`` and ``OPENAPI_PATHS``.

Each handler takes the session entry and returns ``(status, payload)`` for
``serve_mode`` to send. Rewind and revert-file move the work tree, so they
hold the entry the way ``/compact`` does: 409 while a turn runs, and
``turn_active`` set for their duration so no turn starts under them. The diff
read only takes the session lock, so overlapping reads never refuse each other.
"""
from __future__ import annotations

from typing import Any, Callable

from coding_harness.tools.base import WritePlan

Reply = tuple[int, dict[str, Any]]
_READ_WAIT_S = 30.0


def _error(status: int, message: str) -> Reply:
    """The serve error envelope."""
    return status, {"error": {"code": status, "message": message}}


def _exclusive(entry: Any, fn: Callable[[], Reply]) -> Reply:
    """Run ``fn`` under the session lock with turns held off; 409 while one runs."""
    with entry.listeners_lock:
        if entry.turn_active:
            return _error(409, "a turn is running; try after it ends")
        entry.turn_active = True
    try:
        with entry.lock:
            return fn()
    finally:
        with entry.listeners_lock:
            entry.turn_active = False


def _read_locked(entry: Any, fn: Callable[[], Reply]) -> Reply:
    """Run a read under the session lock without claiming ``turn_active``.

    Two reads, or a read and a turn POST, must not refuse each other. A turn
    accepted meanwhile waits on the lock for the read to finish.
    """
    with entry.listeners_lock:
        if entry.turn_active:
            return _error(409, "a turn is running; try after it ends")
    if not entry.lock.acquire(timeout=_READ_WAIT_S):
        return _error(409, "a turn is running; try after it ends")
    try:
        return fn()
    finally:
        entry.lock.release()


def review_gate(entry: Any, plan: WritePlan) -> bool:
    """The registry's confirm callback: park a write for review when the operator asked to."""
    broker = entry.broker
    if not entry.review_writes or broker is None:
        return True
    decision = broker.request(
        tool=plan.tool,
        args={"file_path": str(plan.file_path), "summary": plan.summary, "diff": plan.unified_diff},
        reason="review",
        kind="review",
    )
    if decision.decision == "allow_always":
        # "Apply all" ends review for this session; it grants nothing else.
        entry.review_writes = False
    return decision.allowed


def annotate_resolved(entry: Any, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Settle review state as a review request resolves and put it on the event.

    Runs in the resolving thread before the parked turn resumes, so the GUI's
    review switch learns "apply all" ended review from the same event.
    """
    if kind != "permission_resolved" or payload.get("kind") != "review":
        return payload
    if payload.get("decision") == "allow_always":
        entry.review_writes = False
    return {**payload, "review_writes": entry.review_writes}


def _turn_arg(value: Any) -> int | None:
    """A positive turn number, None for absent; raises ValueError otherwise."""
    if value in (None, "", "all"):
        return None
    turn = int(value)
    if turn < 1:
        raise ValueError(turn)
    return turn


def get_diff(entry: Any, query: dict[str, list[str]]) -> Reply:
    """GET /v1/sessions/{id}/diff?turn=N: the turn's (or session's) changed files."""
    try:
        turn = _turn_arg((query.get("turn") or [None])[0])
    except (TypeError, ValueError):
        return _error(400, "turn must be a positive integer")
    return _read_locked(entry, lambda: (200, entry.session.diff(turn)))


def post_rewind(entry: Any, body: dict[str, Any]) -> Reply:
    """POST /v1/sessions/{id}/rewind {"turns": n}: undo the last n turns, disk and history."""
    turns = body.get("turns", 1)
    if not isinstance(turns, int) or isinstance(turns, bool) or turns < 1:
        return _error(400, "body.turns must be a positive integer")

    def _run() -> Reply:
        entry.session.rewind(turns)
        report = dict(entry.session.last_rewind)
        if report.get("failed"):
            return 500, {"error": {"code": 500, "message": "rewind failed; history kept"}, **report}
        return 200, report
    return _exclusive(entry, _run)


def post_revert_file(entry: Any, body: dict[str, Any]) -> Reply:
    """POST /v1/sessions/{id}/revert-file {"path", "turn"}: put one file back."""
    path = body.get("path")
    if not isinstance(path, str) or not path:
        return _error(400, "body.path must be a non-empty string")
    try:
        turn = _turn_arg(body.get("turn"))
    except (TypeError, ValueError):
        return _error(400, "body.turn must be a positive integer")

    def _run() -> Reply:
        if not entry.session.revert_file(path, turn):
            return _error(404, f"nothing to revert for {path}")
        return 200, {"path": path, "turn": turn, "reverted": True}
    return _exclusive(entry, _run)


def post_review(entry: Any, body: dict[str, Any]) -> Reply:
    """POST /v1/sessions/{id}/review {"enabled": bool}: park every write for approval."""
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return _error(400, "body.enabled must be a boolean")
    if enabled and entry.broker is None:
        return _error(409, "session has no permission broker (create with interactive:true)")
    entry.review_writes = enabled
    return 200, {"review_writes": enabled}


POST_ROUTES: dict[str, Callable[[Any, dict[str, Any]], Reply]] = {
    "rewind": post_rewind,
    "revert-file": post_revert_file,
    "review": post_review,
}


def _op(summary: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """One OpenAPI operation on a session route."""
    op: dict[str, Any] = {
        "summary": summary,
        "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
        "responses": {"200": {"description": "ok"}, "404": {"description": "unknown session"},
                      "409": {"description": "a turn is running"}},
    }
    if body is not None:
        op["requestBody"] = {"content": {"application/json": {"schema": {"type": "object", "properties": body}}}}
    return op


OPENAPI_PATHS: dict[str, Any] = {
    "/v1/sessions/{id}/diff": {"get": _op("Changed files for ?turn=N, or the whole session")},
    "/v1/sessions/{id}/rewind": {"post": _op(
        "Undo the last n turns on disk and in history", {"turns": {"type": "integer"}})},
    "/v1/sessions/{id}/revert-file": {"post": _op(
        "Put one file back as it was before a turn",
        {"path": {"type": "string"}, "turn": {"type": "integer"}})},
    "/v1/sessions/{id}/review": {"post": _op(
        "Park every Write/Edit for operator approval", {"enabled": {"type": "boolean"}})},
}
