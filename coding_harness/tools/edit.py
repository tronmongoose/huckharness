"""Edit tool: string replacement in an existing file, with a whitespace tolerance ladder.

Mirrors Claude Code's Edit tool. Finds ``old_string`` in the file (exact
first, then trailing-whitespace and indentation tolerant, each requiring a
unique hit) and replaces it with ``new_string``. Accepts an ``edits`` list
for several sequential replacements applied all-or-nothing. Sentinel-gated
via the registry; uses the ``PlannedTool`` plan/apply split so the registry
can show a unified diff and capture a snapshot before any disk write.
"""
from __future__ import annotations

import difflib
import os
from pathlib import Path

from .base import PlannedTool, ToolResult, WritePlan, diff_label
from .edit_match import TIERS, Match, locate, nearest_window

CONTEXT_LINES = 3  # lines of context around the replacement in the preview
DIFF_CONTEXT = 3
MAX_REPLACEMENTS = 50
MIN_REPLACE_ALL_CHARS = 3

_EDIT_PAIR = {
    "type": "object",
    "properties": {
        "old_string": {"type": "string", "description": "The text to find in the file."},
        "new_string": {"type": "string", "description": "The replacement text."},
    },
    "required": ["old_string", "new_string"],
}


class Edit(PlannedTool):
    name = "Edit"
    category = "edit"
    description = (
        "Replace old_string with new_string in an existing file. The match is "
        "exact first; a unique match that differs only in trailing whitespace or "
        "in indentation is also accepted. Fails if old_string is not found or is "
        "ambiguous (appears more than once) unless replace_all is true. Pass "
        "edits=[{old_string, new_string}, ...] to apply several replacements to "
        "one file in order, all-or-nothing. Use absolute paths only."
    )
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file to edit.",
            },
            "old_string": {
                "type": "string",
                "description": "The text to find in the file (single-edit form).",
            },
            "new_string": {
                "type": "string",
                "description": "The replacement text (single-edit form).",
            },
            "edits": {
                "type": "array",
                "items": _EDIT_PAIR,
                "description": "Sequential replacements, applied in order, all-or-nothing. Replaces old_string/new_string.",
            },
            "replace_all": {
                "type": "boolean",
                "description": "Replace all occurrences (default: false, fail if >1 match). old_string needs at least 3 non-space characters.",
                "default": False,
            },
        },
        "required": ["file_path"],
    }

    def plan(self, args: dict) -> WritePlan | ToolResult:
        raw_path = args.get("file_path")
        replace_all = bool(args.get("replace_all", False))
        if not isinstance(raw_path, str) or not raw_path:
            return ToolResult(content="error: file_path required (string)", is_error=True)
        edits = _edits_from_args(args)
        if isinstance(edits, ToolResult):
            return edits
        target = _read_target(raw_path)
        if isinstance(target, ToolResult):
            return target
        path, pre_image = target
        old_text = pre_image.decode("utf-8", errors="replace")

        applied = _apply_all(path, old_text, edits, replace_all)
        if isinstance(applied, ToolResult):
            return applied
        text, replacements, tiers, notes, inserted = applied
        return WritePlan(
            tool=self.name,
            file_path=path,
            existed=True,
            pre_image=pre_image,
            post_image=text.encode("utf-8"),
            unified_diff=_unified_diff(old_text, text, diff_label(path)),
            summary=f"Edit {path} ({_plural(replacements)})",
            metadata={
                "replacements": replacements,
                "match_tier": max(tiers, key=TIERS.index),
                "file": str(path),
                "new_string": inserted,
                "notes": notes,
            },
        )

    def apply(self, plan: WritePlan) -> ToolResult:
        path = plan.file_path
        try:
            path.write_bytes(plan.post_image)
        except OSError as e:
            return ToolResult(content=f"error: write failed: {e}", is_error=True)

        new_text = plan.post_image.decode("utf-8", errors="replace")
        preview = _build_preview(new_text, plan.metadata.get("new_string", ""))
        lines = [f"edited {path} ({_plural(plan.metadata.get('replacements', 1))})"]
        lines.extend(plan.metadata.get("notes", []))
        lines.append(preview)
        return ToolResult(
            content="\n".join(lines),
            metadata={k: v for k, v in plan.metadata.items() if k not in ("new_string", "notes")},
        )


def _plural(n: int) -> str:
    """Replacement count with its noun."""
    return f"{n} replacement{'s' if n != 1 else ''}"


def _edits_from_args(args: dict) -> list[tuple[str, str]] | ToolResult:
    """The (old, new) pairs from either the single form or the edits list, exactly one of them."""
    edits = args.get("edits")
    single = "old_string" in args or "new_string" in args
    if edits is None and not single:
        return ToolResult(content="error: old_string and new_string (or edits) required", is_error=True)
    if edits is not None and single:
        return ToolResult(content="error: give either old_string/new_string or edits, not both", is_error=True)
    if edits is None:
        if not isinstance(args.get("old_string"), str):
            return ToolResult(content="error: old_string required (string)", is_error=True)
        if not isinstance(args.get("new_string"), str):
            return ToolResult(content="error: new_string required (string)", is_error=True)
        return [(args["old_string"], args["new_string"])]
    if not isinstance(edits, list) or not edits:
        return ToolResult(content="error: edits must be a non-empty list", is_error=True)
    pairs = []
    for i, item in enumerate(edits, 1):
        ok = isinstance(item, dict) and isinstance(item.get("old_string"), str) and isinstance(item.get("new_string"), str)
        if not ok:
            return ToolResult(content=f"error: edits[{i}] needs old_string and new_string (strings)", is_error=True)
        pairs.append((item["old_string"], item["new_string"]))
    return pairs


def _read_target(raw_path: str) -> tuple[Path, bytes] | ToolResult:
    """Resolve and read the file to edit, or the error explaining why not."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        return ToolResult(content=f"error: must be absolute path, got {raw_path!r}", is_error=True)
    if not path.exists():
        return ToolResult(content=f"error: file not found: {path}", is_error=True)
    if not path.is_file():
        return ToolResult(content=f"error: not a regular file: {path}", is_error=True)
    try:
        return path, path.read_bytes()
    except OSError as e:
        return ToolResult(content=f"error: cannot read file: {e}", is_error=True)


def _apply_all(
    path: Path, text: str, edits: list[tuple[str, str]], replace_all: bool,
) -> tuple[str, int, list[str], list[str], str] | ToolResult:
    """Apply edits in order against the evolving text; any failure or the replacement cap aborts the whole plan."""
    replacements = 0
    tiers: list[str] = []
    notes: list[str] = []
    inserted = ""
    for i, (old, new) in enumerate(edits, 1):
        outcome = _apply_one(path, text, old, new, replace_all)
        if isinstance(outcome, ToolResult):
            return outcome
        text, count, tier, inserted = outcome
        replacements += count
        tiers.append(tier)
        if tier != "exact":
            label = f"edit {i} " if len(edits) > 1 else ""
            notes.append(f"{label}matched with whitespace tolerance (tier {tier})")
    if replacements > MAX_REPLACEMENTS:
        return ToolResult(
            content=(
                f"error: this edit would make {replacements} replacements (limit {MAX_REPLACEMENTS}); "
                f"give a longer old_string or split the change into targeted edits"
            ),
            is_error=True,
        )
    return text, replacements, tiers, notes, inserted


def _apply_one(path: Path, text: str, old: str, new: str, replace_all: bool) -> tuple[str, int, str, str] | ToolResult:
    """Apply one replacement to text: (new_text, count, tier, inserted) or the error explaining why not."""
    if not old:
        return ToolResult(content="error: old_string must not be empty", is_error=True)
    if old == new:
        return ToolResult(content=_identical_message(text, old), is_error=True)
    if replace_all:
        if len(old.strip()) < MIN_REPLACE_ALL_CHARS:
            return ToolResult(
                content="error: old_string too short for replace_all; give at least 3 non-space characters",
                is_error=True,
            )
        count = text.count(old)
        if count > 1:
            return text.replace(old, new), count, "exact", new
    found = locate(text, old, new)
    if isinstance(found, Match):
        return text[:found.start] + found.replacement + text[found.end:], 1, found.tier, found.replacement
    if "exact" in found:
        return ToolResult(content=_ambiguous_message(path, text.count(old)), is_error=True)
    if found:
        return ToolResult(
            content=(
                f"error: old_string not found exactly in {path}, and it matches more than one region "
                f"under whitespace tolerance (tiers {', '.join(found)}). Copy it exactly and add "
                f"surrounding context to make it unique."
            ),
            is_error=True,
        )
    return ToolResult(content=_not_found_message(path, text, old), is_error=True)


def _ambiguous_message(path: Path, count: int) -> str:
    """Error text for an old_string with several exact occurrences and no replace_all."""
    return (
        f"error: old_string appears {count} times in {path}. "
        f"Use replace_all=true to replace all, or provide a longer "
        f"old_string with more surrounding context to make it unique."
    )


def _numbered(text: str, start: int, end: int) -> str:
    """Render lines start..end (1-based, inclusive) with line-number gutters."""
    lines = text.splitlines()
    return "\n".join(f"{i:6d}\t{lines[i - 1]}" for i in range(start, min(end, len(lines)) + 1))


def _not_found_message(path: Path, text: str, old: str) -> str:
    """Error text for a missing old_string, with the closest window when hinting is on."""
    window = nearest_window(text, old) if os.environ.get("HARNESS_EDIT_HINT") != "0" else None
    if window is None:
        snippet = "\n".join(text.split("\n")[:20])
        return f"error: old_string not found in {path}\n--- first 20 lines of file ---\n{snippet}"
    start, end, ratio = window
    return (
        f"error: old_string not found in {path}\n"
        f"closest match at lines {start}-{end} (similarity {ratio:.2f}). "
        f"Copy it exactly, including whitespace:\n{_numbered(text, start, end)}"
    )


def _identical_message(text: str, old: str) -> str:
    """Error text for old_string == new_string, pointing at the region if it is already present."""
    idx = text.find(old)
    if idx < 0:
        return "error: old_string and new_string are identical"
    start = text.count("\n", 0, idx) + 1
    end = start + old.rstrip("\n").count("\n")
    return (
        f"error: old_string and new_string are identical; that text is already present "
        f"at lines {start}-{end}:\n{_numbered(text, start, end)}\n"
        f"If the file already has the change you want, do not edit it again."
    )


def _unified_diff(old: str, new: str, path: str) -> str:
    diff = difflib.unified_diff(
        old.splitlines(keepends=False),
        new.splitlines(keepends=False),
        fromfile=f"a{path}",
        tofile=f"b{path}",
        n=DIFF_CONTEXT,
        lineterm="",
    )
    return "\n".join(diff)


def _build_preview(content: str, new_string: str) -> str:
    """Show a few lines of context around the replacement."""
    lines = content.split("\n")
    new_first_line = new_string.split("\n")[0] if new_string else ""
    target_idx = next((i for i, line in enumerate(lines) if new_first_line and new_first_line in line), None)
    if target_idx is None:
        return ""
    start = max(0, target_idx - CONTEXT_LINES)
    end = min(len(lines), target_idx + CONTEXT_LINES + len(new_string.split("\n")))
    return "--- context ---\n" + "\n".join(f"{i + 1:6d}\t{lines[i]}" for i in range(start, end))
