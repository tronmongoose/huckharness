"""Sibling git worktree setup for ``bjorn --worktree <topic>``.

Creates ``../<repo>-<topic>`` next to the repo root (``-b <topic>`` unless the
branch already exists), chdirs into it, and hands control back to the CLI,
which starts the REPL there. Refuses an existing path so a second session
never lands in a checkout another agent is using.

Exports: WorktreeError, WorktreeExists, create(topic, cwd), enter(topic, cwd).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

GIT_TIMEOUT_S = 10.0


class WorktreeError(RuntimeError):
    """A worktree that cannot be created: not a repo, path taken, git failed."""


class WorktreeExists(WorktreeError):
    """The sibling path is already taken."""


def _git(args: list[str], cwd: str) -> subprocess.CompletedProcess:
    """Run one git command; a hung git becomes a WorktreeError after the timeout."""
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
            timeout=GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as e:
        raise WorktreeError(f"git {args[0]} timed out after {GIT_TIMEOUT_S:.0f}s") from e


def create(topic: str, cwd: str) -> Path:
    """Create ``../<repo>-<topic>`` without leaving ``cwd``; return its path."""
    top = _git(["rev-parse", "--show-toplevel"], cwd)
    if top.returncode != 0:
        raise WorktreeError(f"not a git repository: {cwd}")
    root = Path(top.stdout.strip())
    path = root.parent / f"{root.name}-{topic}"
    if path.exists():
        raise WorktreeExists(f"path already exists: {path}")
    branch_exists = _git(
        ["rev-parse", "--verify", "--quiet", f"refs/heads/{topic}"], cwd,
    ).returncode == 0
    if branch_exists:
        added = _git(["worktree", "add", str(path), topic], cwd)
    else:
        added = _git(["worktree", "add", str(path), "-b", topic], cwd)
    if added.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {added.stderr.strip()}")
    return path


def enter(topic: str, cwd: str) -> Path:
    """Create ``../<repo>-<topic>``, chdir into it, and return its path."""
    path = create(topic, cwd)
    os.chdir(path)
    return path
