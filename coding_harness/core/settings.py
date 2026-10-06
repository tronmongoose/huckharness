"""Harness settings file: user-level then project-level JSON, merged per key.

Exports ``Settings``, ``SettingsError``, ``USER_SETTINGS``,
``PROJECT_SETTINGS_REL``, ``LIST_KEYS``, ``resolve_autonomy``,
``load_settings``, ``user_settings_path``, ``project_settings_path``,
``read_settings_file``, ``merge_settings``, ``validate_file`` and
``write_user_settings``.

``load_settings(cwd)`` reads ``~/.config/bjorn/settings.json`` and then
``<git root or cwd>/.bjorn/settings.json``. The later file wins per scalar
key (``autonomy``, ``model``); the list keys are concatenated, so a project
can only add command prefixes and roots, never remove what the user file set. ``commandBlocklist``
extends the built-in blocklist in ``security.policy`` and can never remove an
entry from it. A project file in a directory the user has not trusted
(``core.trust``) contributes only its restrictive keys; the privileged ones
(``PRIVILEGED_KEYS``) are skipped and named in ``Settings.untrusted_keys``.
Unknown keys raise ``SettingsError`` naming the key; a missing
file contributes nothing. ``HARNESS_SETTINGS=off`` skips both files.

``write_user_settings`` is the one writer: it runs the candidate through the
same ``_merge`` the loader uses and replaces the user file atomically, 0600.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coding_harness.core import trust
from coding_harness.core.mode import Autonomy, parse_autonomy

USER_SETTINGS = Path("~/.config/bjorn/settings.json")
PROJECT_SETTINGS_REL = Path(".bjorn") / "settings.json"
LIST_KEYS = {
    "commandAllowlist": "command_allowlist",
    "commandDenylist": "command_denylist",
    "commandBlocklist": "command_blocklist",
    "denyWrite": "deny_write",
    "extraReadRoots": "extra_read_roots",
}
_RAW_KEYS = {"sandbox": dict}
# Keys that widen what a session may do or run code. An untrusted project file
# may not set them.
PRIVILEGED_KEYS = frozenset({"autonomy", "hooks", "commandAllowlist", "extraReadRoots", "sandbox"})
# Every list entry is a prefix or glob checked on each command or write.
MAX_LIST_ITEMS = 500
MAX_ITEM_CHARS = 1024
_MAX_WALK_DEPTH = 25


class SettingsError(ValueError):
    """A settings file that cannot be honored: bad JSON, bad type, unknown key."""


@dataclass
class Settings:
    """Merged settings; every field has the empty default when no file sets it."""

    autonomy: Autonomy | None = None
    model: str | None = None
    command_allowlist: list[str] = field(default_factory=list)
    command_denylist: list[str] = field(default_factory=list)
    command_blocklist: list[str] = field(default_factory=list)
    deny_write: list[str] = field(default_factory=list)
    extra_read_roots: list[str] = field(default_factory=list)
    hooks: dict[str, Any] = field(default_factory=dict)
    sandbox: dict[str, Any] = field(default_factory=dict)
    brain: dict[str, Any] = field(default_factory=dict)  # MCP server block for slos-recall
    models: dict[str, str] = field(default_factory=dict)  # role -> tag, see core/model_roles
    untrusted_keys: list[str] = field(default_factory=list)  # project keys skipped for lack of trust


def resolve_autonomy(explicit: Autonomy | None, settings: Settings) -> Autonomy:
    """The level a session runs at: the explicit flag, else the file, else LOW."""
    return explicit or settings.autonomy or Autonomy.LOW


def load_settings(cwd: str) -> Settings:
    """User file then project file merged into one ``Settings``."""
    settings = Settings()
    if os.environ.get("HARNESS_SETTINGS") == "off":
        return settings
    _merge(settings, _read(user_settings_path()), user_settings_path())
    project = project_settings_path(cwd)
    _merge(settings, _project_view(settings, _read(project), project), project)
    return settings


def user_settings_path() -> Path:
    """The user settings file, with ``~`` expanded."""
    return Path(os.path.expanduser(str(USER_SETTINGS)))


def project_settings_path(cwd: str) -> Path:
    """The project settings file for ``cwd``'s repository root."""
    return _project_root(cwd) / PROJECT_SETTINGS_REL


def read_settings_file(path: Path) -> dict[str, Any]:
    """The raw JSON object in one settings file; SettingsError when unreadable."""
    return _read(path)


def merge_settings(user: dict[str, Any], project: dict[str, Any], cwd: str) -> Settings:
    """Already-read user and project objects merged as ``load_settings`` would."""
    settings = Settings()
    _merge(settings, user, user_settings_path())
    path = project_settings_path(cwd)
    _merge(settings, _project_view(settings, project, path), path)
    return settings


def validate_file(data: dict[str, Any], path: Path) -> Settings:
    """``data`` merged alone as if read from ``path``; SettingsError on anything invalid."""
    settings = Settings()
    _merge(settings, data, path)
    return settings


def write_user_settings(data: dict[str, Any]) -> Path:
    """Validate ``data`` as the user file, then replace that file atomically at 0600."""
    path = user_settings_path()
    validate_file(data, path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return path


def _project_view(settings: Settings, data: dict[str, Any], path: Path) -> dict[str, Any]:
    """The project file's keys that may apply: all when trusted, else the restrictive ones."""
    if trust.is_trusted(path.parent.parent):
        return data
    settings.untrusted_keys = sorted(k for k in data if k in PRIVILEGED_KEYS)
    return {k: v for k, v in data.items() if k not in PRIVILEGED_KEYS}


def _project_root(cwd: str) -> Path:
    """Nearest ancestor (or cwd) holding a .git entry, else cwd itself."""
    start = Path(cwd).resolve()
    cur = start
    for _ in range(_MAX_WALK_DEPTH):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return start


def _read(path: Path) -> dict[str, Any]:
    """Parsed JSON object at path; empty when the file is absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise SettingsError(f"{path}: {e}") from e
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise SettingsError(f"{path}: invalid JSON ({e})") from e
    if not isinstance(data, dict):
        raise SettingsError(f"{path}: top level must be a JSON object")
    return data


def _merge(settings: Settings, data: dict[str, Any], path: Path) -> None:
    """Apply one file's keys onto settings: lists concatenate, scalars replace."""
    for key, value in data.items():
        if key in LIST_KEYS:
            getattr(settings, LIST_KEYS[key]).extend(_string_list(key, value, path))
        elif key == "autonomy":
            settings.autonomy = _autonomy(value, path)
        elif key == "model":
            if not isinstance(value, str) or not value:
                raise SettingsError(f"{path}: model must be a non-empty string")
            settings.model = value
        elif key == "hooks":
            _merge_hooks(settings, value, path)
        elif key == "models":
            settings.models.update(_models(value, path))
        elif key == "brain":
            # It names a command the server spawns, so a repo's .bjorn file
            # (untrusted input) may not set it; only the user's own file can.
            if path != user_settings_path():
                raise SettingsError(f"{path}: brain may only be set in {USER_SETTINGS}")
            if not isinstance(value, dict):
                raise SettingsError(f"{path}: brain must be a dict")
            if "backend" in value and value["backend"] not in ("mcp", "inprocess"):
                raise SettingsError(f"{path}: brain.backend must be mcp or inprocess")
            settings.brain = value
        elif key in _RAW_KEYS:
            if not isinstance(value, _RAW_KEYS[key]):
                raise SettingsError(f"{path}: {key} must be a {_RAW_KEYS[key].__name__}")
            setattr(settings, key, value)
        else:
            raise SettingsError(f"{path}: unknown settings key {key!r}")


def _models(value: Any, path: Path) -> dict[str, str]:
    """The ``models`` block: a known role name to a non-empty tag."""
    from coding_harness.core.model_roles import ROLES

    if not isinstance(value, dict):
        raise SettingsError(f"{path}: models must be an object of role -> model tag")
    for role, tag in value.items():
        if role not in ROLES:
            raise SettingsError(f"{path}: unknown model role {role!r} (roles: {', '.join(ROLES)})")
        if not isinstance(tag, str) or not tag:
            raise SettingsError(f"{path}: models.{role} must be a non-empty string")
    return dict(value)


def _merge_hooks(settings: Settings, value: Any, path: Path) -> None:
    """hooks is ``{event: [entries]}``; per event the lists concatenate."""
    if not isinstance(value, dict):
        raise SettingsError(f"{path}: hooks must be an object of event lists")
    for event, entries in value.items():
        if not isinstance(entries, list):
            raise SettingsError(f"{path}: hooks[{event!r}] must be a list")
        settings.hooks.setdefault(event, []).extend(entries)


def _string_list(key: str, value: Any, path: Path) -> list[str]:
    """value as a list of strings, or SettingsError."""
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise SettingsError(f"{path}: {key} must be a list of strings")
    if len(value) > MAX_LIST_ITEMS:
        raise SettingsError(f"{path}: {key} holds more than {MAX_LIST_ITEMS} entries")
    if any(len(v) > MAX_ITEM_CHARS for v in value):
        raise SettingsError(f"{path}: {key} has an entry longer than {MAX_ITEM_CHARS} characters")
    return list(value)


def _autonomy(value: Any, path: Path) -> Autonomy:
    """value as an ``Autonomy`` level, or SettingsError."""
    if not isinstance(value, str):
        raise SettingsError(f"{path}: autonomy must be a string")
    try:
        return parse_autonomy(value)
    except ValueError as e:
        raise SettingsError(f"{path}: {e}") from None
