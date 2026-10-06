"""Hierarchical convention-file ingestion for the harness system prompt.

Walks from a working directory up to the git-repo root (bounded), collecting
agent convention files. Prefers AGENTS.md (agent-directed, concise) over
CLAUDE.md (human-oriented, often larger than the local model's whole context).
Files render root-down so closer files override farther ones. A total byte
budget caps the injection with per-layer caps (repo 12000, global 6000,
memory 6000); a file that would overflow its cap is skipped WHOLE, never
truncated mid-rule — a half-injected governance file gives false confidence
(fail closed). Two extra layers wrap the repo files: a global
``~/.config/bjorn/AGENTS.md`` renders first (outermost, overridden by
everything closer), and the per-repo MEMORY.md index renders after them
under its own ``## Memory`` heading.

Exports: GLOBAL_CONVENTIONS, find_convention_files(cwd),
render_conventions(cwd, budget_bytes).
"""
from __future__ import annotations

import os
from pathlib import Path

from coding_harness.context.memory import render_memory

# AGENTS.md is tried first per directory; only if absent do we fall back to
# CLAUDE.md. The local model runs at 8k context, so the concise file wins.
_CONVENTION_NAMES = ("AGENTS.md", "CLAUDE.md")
GLOBAL_CONVENTIONS = Path("~/.config/bjorn/AGENTS.md")
_MAX_WALK_DEPTH = 25
_DEFAULT_BUDGET_BYTES = 24000
_REPO_CAP_BYTES = 12000
_GLOBAL_CAP_BYTES = 6000
_MEMORY_CAP_BYTES = 6000


def _iter_dirs_up(start: Path, max_depth: int = _MAX_WALK_DEPTH):
    """Yield start then each parent, bounded, stopping after the repo root."""
    cur = start
    for _ in range(max_depth):
        yield cur
        if (cur / ".git").exists() or cur.parent == cur:
            return
        cur = cur.parent


def find_convention_files(cwd: str | Path) -> list[Path]:
    """Convention files from repo-root down to cwd (closer files last)."""
    dirs = list(_iter_dirs_up(Path(cwd).resolve()))
    files: list[Path] = []
    for d in reversed(dirs):  # root .. cwd
        for name in _CONVENTION_NAMES:
            p = d / name
            if p.is_file():
                files.append(p)
                break  # one convention file per directory, AGENTS.md preferred
    return files


def _file_blocks(files: list[Path], cap: int) -> tuple[list[str], list[str]]:
    """(blocks under the cap, paths skipped whole). No mid-rule cuts."""
    parts: list[str] = []
    skipped: list[str] = []
    used = 0
    for p in files:
        try:
            text = p.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        block = f"### {p.name} ({p.parent})\n\n{text}"
        if used + len(block.encode()) > cap:
            skipped.append(str(p))
            continue
        parts.append(block)
        used += len(block.encode())
    return parts, skipped


def _conventions_section(cwd: str | Path, budget_bytes: int) -> str:
    """Global-then-repo convention files as one section; empty when none."""
    global_file = Path(os.path.expanduser(str(GLOBAL_CONVENTIONS)))
    global_parts, global_skipped = _file_blocks(
        [global_file] if global_file.is_file() else [],
        min(_GLOBAL_CAP_BYTES, budget_bytes),
    )
    repo_parts, repo_skipped = _file_blocks(
        find_convention_files(cwd), min(_REPO_CAP_BYTES, budget_bytes),
    )
    parts = global_parts + repo_parts
    skipped = global_skipped + repo_skipped
    if not parts and not skipped:
        return ""
    if not parts:
        listed = ", ".join(skipped)
        return (
            "\n\n## Repository conventions\n\n"
            f"Convention files exist but exceed the injection budget: {listed}.\n"
            "Read the closest one before any repo-shaped decision "
            "(commits, file placement, naming).\n"
        )
    note = (
        f"\n\n(Not shown due to size: {', '.join(skipped)}. Read on demand.)"
        if skipped
        else ""
    )
    return (
        "\n\n## Repository conventions (root-down; closer files override)\n\n"
        + "\n\n".join(parts)
        + note
        + "\n"
    )


def render_conventions(
    cwd: str | Path | None = None, budget_bytes: int = _DEFAULT_BUDGET_BYTES
) -> str:
    """Injectable context block: global + repo conventions, then the memory index."""
    cwd = os.getcwd() if cwd is None else cwd
    out = _conventions_section(cwd, budget_bytes)
    memory = render_memory(cwd, min(_MEMORY_CAP_BYTES, budget_bytes))
    if memory:
        out += "\n\n## Memory\n\n" + memory + "\n"
    return out
