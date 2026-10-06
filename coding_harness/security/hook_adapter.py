"""Deployment-hook adapter: runs a ``sentinel-gate.py`` on the legacy contract.

Exports ``HookVerdict``, ``TIMEOUT_S``, ``HOOK_REL``, ``resolve`` and
``review``. The contract is JSON on stdin (``tool_name``, ``tool_input``,
``session_id`` plus the harness-added ``category``), exit 0 allows, exit 2
blocks with the reason on stderr. Everything else fails closed: a timeout, any
other exit code, an unlaunchable interpreter, or a configured hook that is not
on disk.

Resolution is ``SENTINEL_GATE_HOOK`` first, else the nearest
``.claude/hooks/sentinel-gate.py`` walking up from ``cwd``. The copy bundled
with this package is skipped during the walk so a standalone run gets only the
in-process policy while a deployment checkout keeps its own rules and alerts.
A hook in a directory the user has not trusted (``core.trust``) is skipped too:
it is code from the repository, and the in-process policy still applies.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_harness.core import trust

TIMEOUT_S = 15
HOOK_REL = Path(".claude") / "hooks" / "sentinel-gate.py"
_BUNDLED = (Path(__file__).resolve().parents[2] / HOOK_REL).resolve()

# GLOBAL-STATE: per-cwd resolution cache; without it every tool call walks the
# filesystem to the root.
_CACHE: dict[str, Path | None] = {}


@dataclass
class HookVerdict:
    """What the subprocess hook decided; never None once a hook applies."""

    allowed: bool
    reason: str


def resolve(cwd: str, *, allow_bundled: bool = False) -> Path | None:
    """Hook for ``cwd``: the env override, else the nearest deployment hook."""
    override = os.environ.get("SENTINEL_GATE_HOOK")
    if override:
        return Path(override)
    key = os.path.realpath(cwd)
    if key not in _CACHE:
        _CACHE[key] = _walk_up(Path(key))
    found = _CACHE[key]
    if found is None and allow_bundled and _BUNDLED.is_file():
        return _BUNDLED
    return found


def _walk_up(start: Path) -> Path | None:
    """Nearest hook at or above ``start`` that is not the bundled copy."""
    for parent in (start, *start.parents):
        candidate = parent / HOOK_REL
        if candidate.is_file() and candidate.resolve() != _BUNDLED and trust.is_trusted(parent):
            return candidate
    return None


def review(
    tool_name: str,
    category: str | None,
    tool_input: dict[str, Any],
    session_id: str,
    *,
    cwd: str,
    allow_bundled: bool = False,
) -> HookVerdict | None:
    """Run the resolved hook on this call; None when no hook applies to ``cwd``."""
    hook = resolve(cwd, allow_bundled=allow_bundled)
    if hook is None:
        return None
    if not hook.is_file():
        return HookVerdict(False, f"configured sentinel-gate hook missing: {hook}")
    event = {
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
        "category": category,
    }
    try:
        proc = subprocess.run(
            [sys.executable, str(hook)],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            timeout=TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return HookVerdict(False, f"sentinel-gate hook timed out (>{TIMEOUT_S}s)")
    except OSError as e:
        return HookVerdict(False, f"sentinel-gate hook could not be invoked: {e}")
    return _verdict(proc.returncode, proc.stderr or "")


def _verdict(returncode: int, stderr: str) -> HookVerdict:
    """Map the hook's exit status onto allow / block / fail-closed block."""
    if returncode == 0:
        return HookVerdict(True, "approved by hook")
    stderr = stderr.strip()
    if returncode == 2:
        return HookVerdict(False, stderr or "blocked by hook")
    return HookVerdict(
        False,
        f"sentinel-gate hook returned unexpected exit {returncode}: {stderr[:200]}",
    )
