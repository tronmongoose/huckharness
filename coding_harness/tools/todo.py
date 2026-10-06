"""TodoWrite tool: the model's live task list for the current session.

The model sends the whole list on every call; the tool validates it, keeps it
in memory, mirrors it to ``meta_dir()/plans/<session_id>.todo.json`` so serve
can answer ``GET .../todo`` after a reload, and returns a one-line summary.
The registry emits ``todo_update`` from the result metadata. It touches no
project file, so it is plan-safe (``core.mode.BUILTIN_PLAN_SAFE``).

Exports: TodoWrite, validate_items(raw), todo_path_for(session_id),
load_todos(session_id), summarize(items), STATUSES.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from coding_harness.core.paths import meta_dir

from .base import Tool, ToolResult

STATUSES = ("pending", "in_progress", "done")
MAX_ITEMS = 100
MAX_TEXT = 500
MAX_ID = 64


def todo_path_for(session_id: str) -> Path:
    """Mirror file for ``session_id``'s list, beside its plan."""
    return meta_dir() / "plans" / f"{session_id}.todo.json"


def _check_item(i: int, item: Any, seen: set[str]) -> str | None:
    """Why item ``i`` is invalid, or None when it is a well-formed todo."""
    if not isinstance(item, dict):
        return f"items[{i}] must be an object"
    extra = set(item) - {"id", "text", "status"}
    if extra:
        return f"items[{i}] has unknown keys: {', '.join(sorted(extra))}"
    tid, text, status = item.get("id"), item.get("text"), item.get("status")
    if not isinstance(tid, str) or not tid.strip() or len(tid) > MAX_ID:
        return f"items[{i}].id must be a non-empty string of at most {MAX_ID} chars"
    if tid in seen:
        return f"items[{i}].id {tid!r} is a duplicate"
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        return f"items[{i}].text must be a non-empty string of at most {MAX_TEXT} chars"
    if status not in STATUSES:
        return f"items[{i}].status must be one of {', '.join(STATUSES)}"
    seen.add(tid)
    return None


def validate_items(raw: Any) -> tuple[list[dict[str, str]] | None, str | None]:
    """(items, None) for a valid list, else (None, reason). Model output is untrusted."""
    if not isinstance(raw, list):
        return None, "items must be a list"
    if len(raw) > MAX_ITEMS:
        return None, f"at most {MAX_ITEMS} items"
    seen: set[str] = set()
    for i, item in enumerate(raw):
        problem = _check_item(i, item, seen)
        if problem is not None:
            return None, problem
    return [{"id": t["id"], "text": t["text"], "status": t["status"]} for t in raw], None


def summarize(items: list[dict[str, str]]) -> str:
    """One line such as '3 items: 1 done, 1 in progress, 1 pending'."""
    counts = {s: sum(1 for t in items if t["status"] == s) for s in STATUSES}
    noun = "item" if len(items) == 1 else "items"
    return (f"{len(items)} {noun}: {counts['done']} done, "
            f"{counts['in_progress']} in progress, {counts['pending']} pending")


def load_todos(session_id: str) -> list[dict[str, str]]:
    """The mirrored list for ``session_id``; empty when absent or unreadable."""
    try:
        raw = json.loads(todo_path_for(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items, _ = validate_items(raw.get("items") if isinstance(raw, dict) else None)
    return items or []


def _mirror(session_id: str, items: list[dict[str, str]]) -> None:
    """Write the list atomically so a reader never sees half a file."""
    path = todo_path_for(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"items": items}), encoding="utf-8")
    os.replace(tmp, path)


class TodoWrite(Tool):
    """Replace the session's task list and report it back in one line."""

    name = "TodoWrite"
    category = "read"
    description = (
        "Record your task list for this session. Send the WHOLE list every "
        "call; it replaces the previous one. Mark exactly the step you are "
        "working on in_progress and finished steps done."
    )
    parameters = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Stable short id, e.g. '1'."},
                        "text": {"type": "string", "description": "What the step does."},
                        "status": {"type": "string", "enum": list(STATUSES)},
                    },
                    "required": ["id", "text", "status"],
                },
            },
        },
        "required": ["items"],
    }

    def __init__(self, session_id: Callable[[], str]):
        """``session_id`` is read per call: the registry learns it after build."""
        self._session_id = session_id
        self.items: list[dict[str, str]] = []

    def run(self, args: dict[str, Any]) -> ToolResult:
        """Validate, store, mirror, and summarize the new list."""
        items, problem = validate_items(args.get("items"))
        if items is None:
            return ToolResult(content=f"error: {problem}", is_error=True)
        self.items = items
        _mirror(self._session_id(), items)
        return ToolResult(content=summarize(items), metadata={"todo_items": items})
