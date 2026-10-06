"""Recall, skill-suggest, memory-proposal and pin routes for serve.

Routes (``{id}`` is a live session):
  GET  /v1/skills/suggest?q=          -> {suggestions: [{name, description, score}]}
  GET  /v1/sessions/{id}/memories     -> {proposals: [{id, name, description, type, text}],
                                          pinned: [{path, tier}]}
  POST /v1/sessions/{id}/memories     {"id", "decision": approve|edit|reject, "sha256",
                                       "text"?}; sha256 is of the text shown, 409 on mismatch
                                      -> {id, decision, path?}; memory_decided
  POST /v1/sessions/{id}/pin          {"path", "pinned": bool} -> {path, pinned, tier?};
                                      note_pinned

serve_mode only dispatches here. Approve runs the operator's PreToolUse hooks
(``core.hooks.build_runner`` over the server's settings) with a ``Write``
payload before the file lands, so the memory schema gate judges it.

Exports: GET_ROUTES, POST_ROUTES, OPENAPI_PATHS, suggest(), handle().
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from coding_harness.context import recall
from coding_harness.context.brain import BrainError, client_for
from coding_harness.context.skills import index_skills, score_skills
from coding_harness.core import hooks as hook_runner
from coding_harness.core import memory_store

GET_ROUTES = frozenset({"memories"})
POST_ROUTES = frozenset({"memories", "pin"})
MAX_QUERY = 2000
MAX_PATH = 512
SKILLS_TTL_S = 5.0
# GLOBAL-STATE: the suggest route answers keystrokes; rereading every SKILL.md
# on each one is wasted work, so the index is kept per cwd for a few seconds.
_SKILLS_CACHE: dict[str, tuple[float, list[Any]]] = {}
_SKILLS_LOCK = threading.Lock()

_ID = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]


def _op(summary: str, code: str, what: str, params: Any = None) -> dict[str, Any]:
    """One OpenAPI operation."""
    return {"summary": summary, "parameters": _ID if params is None else params,
            "responses": {code: {"description": what}}}


OPENAPI_PATHS: dict[str, Any] = {
    "/v1/skills/suggest": {
        "get": _op("Up to three skills matching q: {suggestions}", "200", "suggestions",
                   [{"name": "q", "in": "query", "schema": {"type": "string"}}]),
    },
    "/v1/sessions/{id}/memories": {
        "get": _op("Pending memory proposals and pinned notes", "200", "proposals"),
        "post": _op("Decide a proposal: body {id, decision, sha256, text?}", "200", "decided"),
    },
    "/v1/sessions/{id}/pin": {
        "post": _op("Pin or unpin a brain note: body {path, pinned}", "200", "pinned"),
    },
}


def _skills(cwd: str) -> list[Any]:
    """The skill index for ``cwd``, reread at most every ``SKILLS_TTL_S`` seconds."""
    now = time.monotonic()
    with _SKILLS_LOCK:
        hit = _SKILLS_CACHE.get(cwd)
        if hit is not None and now - hit[0] < SKILLS_TTL_S:
            return hit[1]
    skills = index_skills(cwd)
    with _SKILLS_LOCK:
        _SKILLS_CACHE[cwd] = (now, skills)
    return skills


def suggest(h: Any, q: str) -> None:
    """Answer GET /v1/skills/suggest with the scored skills for ``q``."""
    ranked = score_skills(q[:MAX_QUERY], _skills(os.getcwd())) if q.strip() else []
    h._send_json(200, {"suggestions": [
        {"name": s.name, "description": s.description, "score": score} for s, score in ranked]})


def _pinned_rows(session: Any) -> list[dict[str, Any]]:
    """The session's pins without their note text."""
    return [{"path": p["path"], "tier": p["tier"]} for p in session.pinned]


def _get_memories(h: Any, entry: Any, sid: str) -> None:
    """List the session's proposals and pins."""
    h._send_json(200, {"session_id": sid, "proposals": memory_store.list_proposals(sid),
                       "pinned": _pinned_rows(entry.session)})


def _post_memories(h: Any, state: Any, entry: Any, sid: str, body: dict[str, Any]) -> None:
    """Apply one decision; hooks come from the server's settings, as a Write's would."""
    session = entry.session
    cwd = os.getcwd()
    runner = session.registry.hooks or hook_runner.build_runner(
        state.settings, session_id=sid, cwd=cwd)
    try:
        out = memory_store.decide(sid, body.get("id"), body.get("decision"), body.get("text"),
                                  cwd, seen=body.get("sha256"), hooks=runner, emit=session._emit)
    except memory_store.ProposalError as e:
        h._send_error_json(e.status, str(e))
        return
    h._send_json(200, {"session_id": sid, **out})


def _post_pin(h: Any, state: Any, entry: Any, sid: str, body: dict[str, Any]) -> None:
    """Pin (fetching the note, enforcing its tier) or unpin one brain note."""
    path, pinned = body.get("path"), body.get("pinned", True)
    if not isinstance(path, str) or not path.strip() or len(path) > MAX_PATH:
        h._send_error_json(400, "body.path must be a non-empty note path")
        return
    if not isinstance(pinned, bool):
        h._send_error_json(400, "body.pinned must be a boolean")
        return
    session = entry.session
    out: dict[str, Any] = {"path": path, "pinned": pinned}
    if pinned:
        try:
            client = client_for(state.settings.brain) if state.brain else None
        except BrainError as e:
            h._send_error_json(502, f"could not pin: {e}")
            return
        if client is None:
            h._send_error_json(404, "no second brain configured")
            return
        try:
            out["tier"] = recall.pin_note(session, client, path)["tier"]
        except BrainError as e:
            h._send_error_json(502, f"could not pin: {e}")
            return
    else:
        session.pinned = [p for p in session.pinned if p["path"] != path]
    session._emit("note_pinned", {"path": path, "pinned": pinned, "tier": out.get("tier")})
    h._send_json(200, {"session_id": sid, **out})


def handle(h: Any, state: Any, method: str, entry: Any, sid: str, sub: str,
           body: dict[str, Any] | None = None) -> None:
    """Dispatch one route from ``GET_ROUTES``/``POST_ROUTES``."""
    body = body or {}
    if method == "GET" and sub == "memories":
        _get_memories(h, entry, sid)
    elif method == "POST" and sub == "memories":
        _post_memories(h, state, entry, sid, body)
    elif method == "POST" and sub == "pin":
        _post_pin(h, state, entry, sid, body)
    else:
        h._send_error_json(405, f"{method} not allowed on {sub}")
