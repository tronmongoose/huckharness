"""Composer helpers for the GUI: file mentions and the slash-command menu.

Exports ``list_files``, ``mention_line``, ``list_commands``,
``expand_custom`` and ``BUILTIN_COMMANDS``. serve_mode routes
``GET /v1/files?q=`` and ``GET /v1/commands`` here, and the turn handler
calls ``mention_line`` so ``@path`` tokens reach the model as file names.
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from coding_harness.context import skills
from coding_harness.core.settings import _project_root
from coding_harness.modes import custom_commands

FILE_CACHE_S = 5.0
MAX_FILES = 50
_GIT_TIMEOUT_S = 5.0
# A mention starts a word: "@src/a.py" but not "me@host".
_MENTION = re.compile(r"(?:^|(?<=\s))@([^\s@]+)")
_TRAILING = ",.;:!?)]}'\""

# GLOBAL-STATE: per-cwd `git ls-files` cache so each keystroke is not a fork.
_cache: dict[str, tuple[float, list[str]]] = {}
_cache_lock = threading.Lock()

# The slash commands the GUI composer handles itself (Thread.tsx runSlash).
BUILTIN_COMMANDS: list[dict[str, str]] = [
    {"name": "/model", "usage": "/model <tag|auto>", "help": "switch this session's model"},
    {"name": "/new", "usage": "/new", "help": "start a fresh session"},
    {"name": "/clear", "usage": "/clear", "help": "same as /new"},
    {"name": "/compact", "usage": "/compact", "help": "summarize history to free context"},
    {"name": "/stop", "usage": "/stop", "help": "stop after the current step"},
    {"name": "/skill", "usage": "/skill <name> <task>", "help": "run a task under a skill"},
]


def _git_files(cwd: str) -> list[str]:
    """Tracked plus untracked-but-not-ignored paths under ``cwd``; [] outside git."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=cwd, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    paths = sorted(set(line for line in out.stdout.splitlines() if line))
    # Deleted-but-tracked files and symlinks out of the project are not offered.
    return [p for p in paths if _resolve(cwd, p) is not None]


def _cached_files(cwd: str) -> list[str]:
    """``_git_files`` memoized for ``FILE_CACHE_S`` per cwd."""
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(cwd)
        if hit and now - hit[0] < FILE_CACHE_S:
            return hit[1]
    files = _git_files(cwd)
    with _cache_lock:
        _cache[cwd] = (now, files)
    return files


def _subsequence(needle: str, hay: str) -> bool:
    """True when every char of ``needle`` appears in ``hay`` in order."""
    it = iter(hay)
    return all(ch in it for ch in needle)


def list_files(cwd: str, query: str) -> dict[str, Any]:
    """Paths matching ``query`` as a case-insensitive subsequence, best first."""
    q = query.lower()
    hits = [p for p in _cached_files(cwd) if _subsequence(q, p.lower())]
    # Name matches first, then shorter paths: "app" should find App.tsx early.
    hits.sort(key=lambda p: (q not in p.lower().rsplit("/", 1)[-1], len(p), p))
    return {"files": hits[:MAX_FILES]}


def _resolve(cwd: str, rel: str) -> str | None:
    """``rel`` as a normalized cwd-relative path when it names a file under cwd, else None."""
    root = Path(cwd).resolve()
    try:
        target = (root / rel).resolve()
    except (OSError, RuntimeError):
        return None
    if not target.is_file() or root not in target.parents:
        return None
    return target.relative_to(root).as_posix()


def mention_line(message: str, cwd: str) -> str:
    """``"Attached files: a, b"`` for each ``@path`` naming a file under cwd, else ""."""
    seen: list[str] = []
    for m in _MENTION.finditer(message):
        raw = m.group(1).rstrip(_TRAILING)
        rel = _resolve(cwd, raw) if raw else None
        if rel is not None and rel not in seen:
            seen.append(rel)
    return f"Attached files: {', '.join(seen)}" if seen else ""


def _custom_names(cwd: str) -> list[dict[str, str]]:
    """Custom command files, project first; a project name hides the user one."""
    roots = (_project_root(cwd) / custom_commands.COMMANDS_PROJECT_REL,
             Path(os.path.expanduser(str(custom_commands.COMMANDS_USER))))
    out: dict[str, dict[str, str]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*.md")):
            name = path.stem
            if name in out or not custom_commands._NAME_RE.match(name):
                continue
            try:
                first = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
            except OSError:
                continue
            out[name] = {"name": f"/{name}", "usage": f"/{name} <args>",
                         "help": (first[0] if first else "")[:120], "kind": "custom"}
    return list(out.values())


def list_commands(cwd: str) -> dict[str, Any]:
    """Built-ins with help, then custom commands, then skills as ``/skill <name>``."""
    builtins = [{**c, "kind": "builtin"} for c in BUILTIN_COMMANDS]
    skill_rows = [{"name": f"/skill {s.name}", "usage": f"/skill {s.name} <task>",
                   "help": s.description[:120], "kind": "skill"}
                  for s in skills.index_skills(cwd)]
    return {"commands": builtins + _custom_names(cwd) + skill_rows}


def expand_custom(message: str, cwd: str) -> str:
    """A ``/name args`` custom command expanded to its template; else unchanged."""
    if not message.startswith("/"):
        return message
    parts = message[1:].split(None, 1)
    name = parts[0] if parts else ""
    if not name or any(c["name"] == f"/{name}" for c in BUILTIN_COMMANDS):
        return message
    expanded = custom_commands.expand(message, cwd)
    return expanded if expanded is not None else message
