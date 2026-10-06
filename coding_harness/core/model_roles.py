"""Which model does which job: code, chat, explore, review, summarize.

Exports ``ROLES``, ``DEFAULTS``, ``configure``, ``role_model`` and ``table``.
The ``models`` block in settings assigns a tag per role; a role left out
falls back to ``DEFAULTS``, and a default that is not pulled falls back to
the code model so a machine without the small model still works.

The split is one big model and one small one. Code and review share the big
model: review is the one job the measured bench showed a small model cannot
do (mistral-small3.2 0.811 agreement, granite4.1:8b 0.453), and the big
model is resident for code anyway. Chat, the Explore subagent and history
summaries go to the small model.
"""
from __future__ import annotations

import os
from typing import Any

ROLES = ("code", "chat", "explore", "review", "summarize")
SMALL = "granite4.1:8b"
DEFAULTS = {
    "code": "mistral-small3.2:latest",
    "chat": SMALL,
    "explore": SMALL,
    "review": "mistral-small3.2",
    "summarize": SMALL,
}
# Env overrides that predate the roles block keep winning over it.
_ENV = {"review": "HARNESS_REVIEW_MODEL"}

_configured: dict[str, str] = {}  # GLOBAL-STATE: settings are read once per process, like the rest


def configure(settings: Any) -> None:
    """Adopt a Settings' ``models`` block and its legacy ``model`` key for this process."""
    roles = dict(getattr(settings, "models", None) or {})
    legacy = getattr(settings, "model", None)
    if legacy and "code" not in roles:
        roles["code"] = legacy
    _configured.clear()
    _configured.update({k: v for k, v in roles.items() if k in ROLES})


def _present(tag: str) -> bool:
    from coding_harness.core import review

    return review._tag_present(tag)


def role_model(role: str, *, check_present: bool = True) -> str:
    """The tag for ``role``: env override, then settings, then the default.

    A default (not an explicit choice) that Ollama does not have falls back to
    the code model. Banned origins are refused whatever the source.
    """
    from coding_harness.models.ollama import assert_model_allowed

    if role not in ROLES:
        raise ValueError(f"unknown model role {role!r}; roles are {', '.join(ROLES)}")
    env = os.environ.get(_ENV.get(role, ""), "") if role in _ENV else ""
    chosen = env or _configured.get(role)
    tag = chosen or DEFAULTS[role]
    if not chosen and role != "code" and check_present and not _present(tag):
        tag = role_model("code", check_present=False)
    assert_model_allowed(tag)
    return tag


def table() -> list[dict[str, Any]]:
    """Every role with its tag and where the tag came from, for the GUI."""
    rows = []
    for role in ROLES:
        if role in _ENV and os.environ.get(_ENV[role]):
            source = f"env {_ENV[role]}"
        elif role in _configured:
            source = "settings"
        else:
            source = "default"
        rows.append({"role": role, "model": role_model(role), "source": source})
    return rows
