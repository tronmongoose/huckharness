"""Serve routes for the GUI settings view: read both files, write the user file.

Exports ``WRITABLE``, ``LOCKED``, ``get_settings``, ``put_settings`` and
``OPENAPI_PATHS``.

The GUI may write only the user file and only the keys in ``WRITABLE``.
``brain`` and ``hooks`` name commands the server spawns, so they stay
hand-edited: a PUT naming either is a 400. ``sandbox`` is passed through to
the sandbox unchecked, so it is hand-edited too. Every key outside
``WRITABLE`` is carried over from disk untouched. The project file is never
written from here, and nothing is written while ``HARNESS_SETTINGS=off``.
Saved values apply to sessions created after the save.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from coding_harness.core import settings as settings_mod
from coding_harness.core.settings import LIST_KEYS, Settings, SettingsError

Reply = tuple[int, dict[str, Any]]
WRITABLE = ("autonomy", "model", "models", "commandAllowlist", "commandDenylist",
            "commandBlocklist", "denyWrite", "extraReadRoots")
LOCKED = ("brain", "hooks")


def _error(status: int, message: str) -> Reply:
    """The serve error envelope."""
    return status, {"error": {"code": status, "message": message}}


def _redact(data: dict[str, Any]) -> dict[str, Any]:
    """A copy with the brain block's env values masked; they may hold tokens."""
    brain = data.get("brain")
    if not isinstance(brain, dict) or not isinstance(brain.get("env"), dict):
        return data
    masked = {k: "***" for k in brain["env"]}
    return {**data, "brain": {**brain, "env": masked}}


def _as_json(s: Settings) -> dict[str, Any]:
    """Merged settings under the file's own key names."""
    out: dict[str, Any] = {
        "autonomy": s.autonomy.value if s.autonomy else None,
        "model": s.model,
        "sandbox": s.sandbox,
        "hooks": s.hooks,
        "brain": s.brain,
        "models": s.models,
    }
    for key, attr in LIST_KEYS.items():
        out[key] = getattr(s, attr)
    return _redact(out)


def _read(path: Any) -> tuple[dict[str, Any], str | None]:
    """(file contents, None) or ({}, the loader's error)."""
    try:
        return settings_mod.read_settings_file(path), None
    except SettingsError as e:
        return {}, str(e)


def get_settings(cwd: str) -> Reply:
    """GET /v1/settings: both files, their merge, and which keys are locked."""
    user_path = settings_mod.user_settings_path()
    project_path = settings_mod.project_settings_path(cwd)
    user, user_err = _read(user_path)
    project, project_err = _read(project_path)
    errors = [e for e in (user_err, project_err) if e]
    effective: dict[str, Any] = {}
    if not errors:
        try:
            effective = _as_json(settings_mod.merge_settings(user, project, cwd))
        except SettingsError as e:
            errors.append(str(e))
    return 200, {
        "user": _redact(user), "project": _redact(project), "effective": effective,
        "user_path": str(user_path), "project_path": str(project_path),
        "locked": list(LOCKED), "writable": list(WRITABLE),
        "errors": errors, "disabled": os.environ.get("HARNESS_SETTINGS") == "off",
    }


def _candidate(body: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """The user file to write: locked keys kept from disk, writable keys from the body."""
    user = body.get("user")
    if not isinstance(user, dict):
        return None, "body.user must be an object"
    for key in user:
        if key in LOCKED:
            return None, (f"{key} names a command the server spawns; edit "
                          f"{settings_mod.USER_SETTINGS} by hand")
        if key not in WRITABLE:
            return None, f"{key!r} is not a writable settings key"
    current, err = _read(settings_mod.user_settings_path())
    if err:
        return None, err
    kept = {k: v for k, v in current.items() if k not in WRITABLE}
    given = {k: v for k, v in user.items() if v is not None}
    return {**kept, **given}, None


def _model_problem(candidate: dict[str, Any]) -> str | None:
    """The banned-origin refusal for the candidate's model or any role model, if any."""
    roles = candidate.get("models")
    tags = [candidate.get("model")] + (list(roles.values()) if isinstance(roles, dict) else [])
    from coding_harness.models.ollama import BannedModelError, assert_model_allowed
    for tag in tags:
        if not isinstance(tag, str):
            continue
        try:
            assert_model_allowed(tag)
        except BannedModelError as e:
            return str(e)
    return None


def put_settings(body: dict[str, Any], cwd: str,
                 apply: Callable[[Settings], None]) -> Reply:
    """PUT /v1/settings: validate, write the user file atomically, reload for new sessions."""
    if os.environ.get("HARNESS_SETTINGS") == "off":
        return _error(409, "settings disabled")
    candidate, problem = _candidate(body)
    if candidate is None:
        return _error(400, problem or "invalid body")
    problem = _model_problem(candidate)
    if problem:
        return _error(400, problem)
    try:
        settings_mod.write_user_settings(candidate)
    except SettingsError as e:
        return _error(400, str(e))
    try:
        apply(settings_mod.load_settings(cwd))
    except SettingsError as e:
        return 200, {"saved": True, "applied": False, "error": str(e)}
    return get_settings(cwd)


OPENAPI_PATHS: dict[str, Any] = {
    "/v1/settings": {
        "get": {
            "summary": "User and project settings files and their merge",
            "responses": {"200": {"description": (
                "{user, project, effective, user_path, project_path, locked, "
                "writable, errors, disabled}")}},
        },
        "put": {
            "summary": "Replace the writable keys of the user settings file",
            "description": (
                "Writes only the user file and only " + ", ".join(WRITABLE) + ". "
                "brain and hooks are 400: they name commands the server spawns. "
                "sandbox and other keys are kept from disk. 409 when HARNESS_SETTINGS=off. "
                "Applies to sessions created after the save."),
            "requestBody": {"content": {"application/json": {"schema": {
                "type": "object", "required": ["user"],
                "properties": {"user": {"type": "object"}},
            }}}},
            "responses": {"200": {"description": "saved; same shape as GET"},
                          "400": {"description": "locked key, unknown key or invalid value"},
                          "409": {"description": "settings disabled"}},
        },
    },
}
