"""Serve routes for the GUI history list: search, rename, delete past sessions.

Exports ``list_history``, ``post_transcript`` and ``OPENAPI_PATHS``.

Each handler returns ``(status, payload)`` for ``serve_mode`` to send. Ids are
checked against ``transcripts.valid_id`` before any path is built, and a
session live in this server is never deleted under itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from coding_harness.core import paths, transcripts
from coding_harness.modes import serve_gui

Reply = tuple[int, dict[str, Any]]
LIST_LIMIT = 30


def _error(status: int, message: str) -> Reply:
    """The serve error envelope."""
    return status, {"error": {"code": status, "message": message}}


def _row(path: Any, prompt: str) -> dict[str, Any]:
    """One history row: the sidecar title, else the prompt's first line."""
    info = transcripts.summary(path)
    title = (transcripts.custom_title(path.stem)
             or serve_gui.skill_title(prompt) or prompt.strip().splitlines()[0][:80])
    # prompt is non-blank here: list_history skips blank first prompts.
    return {"id": path.stem, "session_id": path.stem, "title": title,
            "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                        .strftime("%Y-%m-%dT%H:%M:%SZ"), **info}


def list_history(cwd: str, live: set[str], q: str = "") -> dict[str, Any]:
    """Past sessions started in ``cwd``, not live, newest first; ``q`` filters."""
    candidates = (transcripts.search(cwd, q, limit=LIST_LIMIT + len(live)) if q.strip()
                  else transcripts.recent(cwd, limit=LIST_LIMIT + len(live)))
    out: list[dict[str, Any]] = []
    for path in candidates:
        if path.stem in live:
            continue
        prompt = transcripts.first_prompt(path)
        if not prompt or not prompt.strip():
            continue  # opened and closed without a real turn: nothing to reopen
        out.append(_row(path, prompt))
        if len(out) >= LIST_LIMIT:
            break
    return {"transcripts": out}


def post_transcript(tail: str, body: dict[str, Any], live: set[str], cwd: str) -> Reply:
    """POST /v1/transcripts/{id}/title or /delete."""
    session_id, _, action = tail.partition("/")
    if not transcripts.valid_id(session_id):
        return _error(400, "invalid session id")
    try:
        if action == "title":
            return 200, {"id": session_id,
                         "title": transcripts.set_title(session_id, body.get("title"))}
        if action == "delete":
            removed = transcripts.delete(session_id, live, cwd)
            return 200, {"id": session_id, "removed": _relative_all(removed)}
    except transcripts.DeleteFailed as e:
        status, payload = _error(500, str(e))
        return status, {**payload, "removed": _relative_all(e.removed),
                        "failed": _relative_all([e.failed])[0]}
    except transcripts.TranscriptError as e:
        return _error(e.status, str(e))
    return _error(404, f"not found: /v1/transcripts/{tail}")


def _relative_all(found: list[Any]) -> list[str]:
    """Paths relative to the state root, so replies never carry home paths."""
    root = paths.meta_dir()
    out = []
    for path in found:
        try:
            out.append(path.relative_to(root).as_posix())
        except ValueError:
            out.append(path.name)
    return out


_ID_PARAM = [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}]

OPENAPI_PATHS: dict[str, Any] = {
    "/v1/transcripts": {
        "get": {
            "summary": "Past sessions in this project, newest first",
            "parameters": [{"name": "q", "in": "query", "required": False,
                            "schema": {"type": "string"}}],
            "responses": {"200": {"description": (
                "{transcripts: [{id, title, first_prompt, started, turns, bytes, "
                "resumable, reason_if_not}]}")}},
        }
    },
    "/v1/transcripts/{id}/title": {
        "post": {
            "summary": "Set (or with an empty string clear) a session's title",
            "parameters": _ID_PARAM,
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "required": ["title"],
                "properties": {"title": {"type": "string", "maxLength": transcripts.TITLE_MAX}},
            }}}},
            "responses": {"200": {"description": "saved"}, "400": {"description": "bad id or title"},
                          "404": {"description": "no transcript"}},
        }
    },
    "/v1/transcripts/{id}/delete": {
        "post": {
            "summary": "Delete a past session: transcript, title, checkpoints, shadow repo",
            "parameters": _ID_PARAM,
            "responses": {"200": {"description": "{id, removed: [paths under the state root]}"},
                          "400": {"description": "bad id"}, "404": {"description": "no transcript"},
                          "409": {"description": "live, or another project's session"},
                          "500": {"description": "stopped part way: {removed, failed}"}},
        }
    },
}
