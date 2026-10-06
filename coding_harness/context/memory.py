"""Per-repo persistent memory under <memory root>/<repo-slug>/.

The memory root is HARNESS_MEMORY_DIR when set, else ~/.config/bjorn/memory.

The model writes memories with the ordinary Write tool (the envelope preset
grants write under this directory from LOW up). MEMORY.md is the index; it is
injected into the system prompt by ``context.conventions``.

Exports: MEMORY_ROOT, memory_root(), repo_slug(cwd), memory_dir(cwd),
render_memory(cwd, budget_bytes).
"""
from __future__ import annotations

import os
from pathlib import Path

MEMORY_ROOT = Path("~/.config/bjorn/memory")
_MAX_WALK_DEPTH = 25


def repo_slug(cwd: str | Path) -> str:
    """Basename of the git root above ``cwd``, else the basename of cwd."""
    start = Path(cwd).resolve()
    cur = start
    for _ in range(_MAX_WALK_DEPTH):
        if (cur / ".git").exists():
            return cur.name
        if cur.parent == cur:
            break
        cur = cur.parent
    return start.name


def memory_root() -> Path:
    """HARNESS_MEMORY_DIR (expanded) when set, else the expanded MEMORY_ROOT."""
    override = os.environ.get("HARNESS_MEMORY_DIR", "").strip()
    return Path(os.path.expanduser(override or str(MEMORY_ROOT)))


def memory_dir(cwd: str | Path) -> Path:
    """This repo's memory directory. Not created here — Write creates parents."""
    return memory_root() / repo_slug(cwd)


def render_memory(cwd: str | Path, budget_bytes: int) -> str:
    """MEMORY.md's text under ``budget_bytes``; empty string when absent.

    An oversized index is cut at the last full line inside the budget — the
    file is one-line entries by convention, so a line cut loses whole entries,
    never half of one.
    """
    path = memory_dir(cwd) / "MEMORY.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if not text:
        return ""
    if len(text.encode()) <= budget_bytes:
        return text
    clipped = text.encode()[:budget_bytes]
    head, _, _ = clipped.rpartition(b"\n")
    return head.decode("utf-8", errors="ignore").strip()
