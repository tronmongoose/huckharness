"""GUI-only serve helpers, kept out of serve_mode's routing.

Exports ``SKILL_MARK``, ``list_skills``, ``skill_prompt``, ``skill_title``,
``transcript_events``, ``list_projects``,
``open_project``, ``brain_status``, ``brain_unavailable``, ``brain_search``, ``brain_page``,
``attach_notes``, ``skill_detail`` and ``save_skill``. serve_mode owns
the HTTP surface; this module owns what those GUI routes compute.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from coding_harness.context import skill_buckets, skills

# First line of a prompt built from a skill. The thread and the session title
# read it back, so a replayed transcript renders the same as the live turn.
SKILL_MARK = "[skill: "
# Only main checkouts directly under here are offered. Fleet worktrees (a .git
# file, not a directory) would bury the dozen real repos under hundreds.
PROJECTS_ROOT = Path(os.environ.get("HARNESS_PROJECTS_ROOT", "~/projects"))
_TASK_SEP = "\n\n---\n\nTask: "
# Separates a prompt from the notes attached to it; the thread shows chips.
ATTACH_MARK = "\n\n---\n\nAttached notes:\n\n"


def _source(path: Path) -> str:
    """Which root a skill came from, in the words a person would use."""
    resolved = path.resolve()
    if resolved.is_relative_to(_bjorn_skills_root()):
        return "bjorn"
    for extra in skills.SKILLS_USER_EXTRA:
        root = Path(os.path.expanduser(str(extra))).resolve()
        if resolved.is_relative_to(root):
            dotted = [part for part in root.parts if part.startswith(".")]
            return dotted[-1].lstrip(".") if dotted else root.name
    return "project"


def list_skills(cwd: str) -> dict[str, Any]:
    """Every indexed skill for ``cwd`` with its bucket, plus the ordered bucket counts."""
    by_name = skill_buckets.user_buckets()
    rows = [
        {"name": s.name, "description": s.description, "source": _source(s.path),
         "bucket": skill_buckets.bucket_of(s.name, s.category, by_name)}
        for s in skills.index_skills(cwd)
    ]
    return {"skills": rows,
            "buckets": skill_buckets.bucket_counts([r["bucket"] for r in rows])}


def skill_prompt(name: str, task: str, cwd: str) -> str | None:
    """The turn prompt for running ``task`` under skill ``name``; None if no such skill."""
    text = skills.load_skill(name, cwd=cwd)
    if text is None:
        return None
    head = f"{SKILL_MARK}{name}]\n{text.rstrip()}"
    return f"{head}{_TASK_SEP}{task}" if task.strip() else head


def skill_title(prompt: str) -> str | None:
    """A session title for a skill prompt: the skill and the task's first line."""
    if not prompt.startswith(SKILL_MARK):
        return None
    name = prompt[len(SKILL_MARK):].split("]", 1)[0]
    task = prompt.split(_TASK_SEP, 1)[1] if _TASK_SEP in prompt else ""
    first = task.strip().splitlines()[0] if task.strip() else ""
    return f"{name}: {first}"[:80] if first else name


def _tool_calls(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """tool_call_id -> tool_call_start payload for one assistant_message record."""
    calls: dict[str, dict[str, Any]] = {}
    for call in record.get("tool_calls") or []:
        fn = call.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments")}
        calls[str(call.get("id"))] = {"event": "tool_call_start", "tool": fn.get("name", "?"),
                                      "args": args if isinstance(args, dict) else {}}
    return calls


_PASS_THROUGH = {"turn_start", "route_decision", "checks_baseline", "error", "turn_done"}


def transcript_events(path: Path) -> list[dict[str, Any]]:
    """Rebuild the live event stream a GUI thread renders from a transcript.

    The transcript keeps assistant_message and tool_result rather than the
    live tool events, so each call is re-emitted as a start/result pair when
    its result appears, matched by tool_call_id.
    """
    events: list[dict[str, Any]] = []
    pending: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("kind")
            body = {k: v for k, v in record.items() if k not in ("kind", "ts")}
            if kind in _PASS_THROUGH:
                events.append({"event": kind, **body})
            elif kind == "assistant_message":
                if record.get("content"):
                    events.append({"event": "assistant_delta", "turn": record.get("turn"),
                                   "text": record["content"]})
                pending.update(_tool_calls(record))
            elif kind == "tool_result":
                start = pending.pop(str(record.get("tool_call_id")), None)
                content = str(record.get("content") or "")
                events.append(start or {"event": "tool_call_start",
                                        "tool": record.get("name", "?"), "args": {}})
                events.append({"event": "tool_call_result", "tool": record.get("name", "?"),
                               "is_error": bool(record.get("is_error")),
                               "content_preview": content[:200], "content": content[:6000]})
    return events


def _repos() -> list[Path]:
    """Main git checkouts one level under PROJECTS_ROOT, by name."""
    root = Path(os.path.expanduser(str(PROJECTS_ROOT)))
    if not root.is_dir():
        return []
    return sorted((d for d in root.iterdir() if (d / ".git").is_dir()),
                  key=lambda d: d.name.lower())


def list_projects(cwd: str) -> dict[str, Any]:
    """Every offered repo, which one this server serves, and which have a live GUI."""
    from coding_harness.modes import ui_mode

    live = ui_mode.live_servers()
    real_cwd = os.path.realpath(cwd)
    return {"projects": [
        {"name": d.name, "path": str(d), "current": os.path.realpath(d) == real_cwd,
         "url": live.get(str(d))}
        for d in _repos()
    ]}


def open_project(path: str, *, model: str | None, force_local: bool) -> tuple[str | None, str]:
    """URL of a GUI server for ``path``, starting one if none runs; (None, reason) otherwise.

    ``path`` comes from the request body, so it must name a listed repo
    exactly. Nothing else is ever used as a working directory.
    """
    from coding_harness.modes import ui_mode

    allowed = {str(d) for d in _repos()}
    if path not in allowed:
        return None, "not_a_listed_project"
    url = ui_mode.live_servers().get(path)
    if url:
        return url, "running"
    url = ui_mode.spawn(path, model=model, force_local=force_local)
    return (url, "started") if url else (None, "did_not_start")


def brain_unavailable(error: str, backend: str | None = None) -> dict[str, Any]:
    """Status for a configured brain that cannot answer."""
    return {"configured": True, "backend": backend, "ok": False, "age_hours": None,
            "stale": False, "upgrade_pending": False, "warnings": [], "error": error}


def brain_status(client: Any) -> dict[str, Any]:
    """Whether a second brain is configured here, answering, and how old its index is.

    In-process the probe resolves the identity's clearance and reads the
    index age, so an unknown agent is not ok. MCP cannot report age, so it
    keeps the one cheap search that proves the child answers.
    """
    if client is None:
        return {"configured": False, "backend": None, "ok": False, "age_hours": None,
                "stale": False, "upgrade_pending": False, "warnings": []}
    from coding_harness.context.brain import BrainError, is_stale
    from coding_harness.context.brain_inprocess import UPGRADE_WARNING

    backend = client.backend_name
    try:
        age = client.probe()
        pending = client.upgrade_pending()
    except BrainError as e:
        return brain_unavailable(str(e), backend)
    warnings = client.last_warnings
    if pending and UPGRADE_WARNING not in warnings:
        warnings.append(UPGRADE_WARNING)
    return {"configured": True, "backend": backend, "ok": True,
            "age_hours": None if age is None else round(age, 1), "stale": is_stale(age),
            "upgrade_pending": pending, "warnings": warnings}


def brain_search(client: Any, query: str, vault: str | None) -> dict[str, Any]:
    """Hits for the person at the screen; no tier cap, the model never sees these."""
    return {"hits": [h.to_dict() for h in client.search(query, vault=vault, top_k=20)]}


def brain_page(client: Any, path: str) -> dict[str, Any]:
    """One note for display."""
    from coding_harness.context.brain import tier_of

    page = client.page(path)
    return {**page, "tier": tier_of(page["sensitivity"])}


def attach_notes(client: Any, paths: list[str], session: Any, message: str) -> str:
    """The prompt with each note appended as a fenced block.

    A confidential or higher note marks the session sensitive *before* the
    turn routes, so this turn and every later one stays on local models.
    Notes go after the prompt, so a skill prompt keeps its first line.
    """
    from coding_harness.context.brain import tier_of

    blocks = []
    for path in paths:
        page = client.page(path)
        tier = tier_of(page["sensitivity"])
        if tier >= 2:
            session.mark_sensitive("attach", tier)
        blocks.append(f"Note {page['path']} ({page['sensitivity'] or 'unlabeled'}):\n"
                      f"```markdown\n{page['content'].rstrip()}\n```")
    return message + ATTACH_MARK + "\n\n".join(blocks)


SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _bjorn_skills_root() -> Path:
    return Path(os.path.expanduser(str(skills.SKILLS_USER))).resolve()


def skill_detail(name: str, cwd: str) -> dict[str, Any] | None:
    """One skill's full text, where it lives, and whether the GUI may edit it."""
    for s in skills.index_skills(cwd):
        if s.name == name:
            try:
                text = s.path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return None
            editable = s.path.resolve().is_relative_to(_bjorn_skills_root())
            return {"name": s.name, "description": s.description, "source": _source(s.path),
                    "bucket": skill_buckets.bucket_of(s.name, s.category),
                    "category": skill_buckets.canonical_bucket(s.category) or "",
                    "editable": editable, "text": text}
    return None


def save_skill(name: str, description: str, body: str, category: str = "") -> tuple[bool, str]:
    """Write ~/.config/bjorn/skills/<name>/SKILL.md; (ok, reason).

    Only the bjorn root is written. The name is checked against a strict
    pattern and the resolved path must stay under that root, so a request
    body can never aim a write anywhere else. A non-empty ``category`` must
    name a bucket, so the frontmatter only ever holds a value the GUI reads.
    """
    if not SKILL_NAME.match(name):
        return False, "name must be lowercase letters, digits and dashes (max 64)"
    if not description.strip() or "\n" in description:
        return False, "description must be one non-empty line"
    bucket = skill_buckets.canonical_bucket(category) if category.strip() else ""
    if bucket is None:
        return False, "category must name a skill bucket"
    root = _bjorn_skills_root()
    target = (root / name / "SKILL.md").resolve()
    if not target.is_relative_to(root):
        return False, "path escapes the skills root"
    target.parent.mkdir(parents=True, exist_ok=True)
    extra = f"category: {bucket}\n" if bucket else ""
    target.write_text(f"---\nname: {name}\ndescription: {description.strip()}\n{extra}---\n\n"
                      f"{body.strip()}\n", encoding="utf-8")
    return True, str(target)
