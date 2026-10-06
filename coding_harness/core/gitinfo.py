"""Read-only queries on the user's git repository for the GUI's git panel.

Every call runs a fixed argv with a timeout in the given directory and fails
soft to ``{"error": ...}``. Nothing from a client reaches these argvs. This is
the operator's repo, unrelated to the ShadowRepo in ``core/git.py``.

Exports: GIT_TIMEOUT_S, run_git(), parse_status(), status(), parse_worktrees(),
worktrees().
"""
from __future__ import annotations

import subprocess
from typing import Any

GIT_TIMEOUT_S = 10.0
_STATUS_ARGV = ["status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all"]
_MAX_ENTRIES = 5000


def run_git(args: list[str], cwd: str, timeout: float = GIT_TIMEOUT_S,
            stdin_text: str = "") -> tuple[int, str, str]:
    """(returncode, stdout, stderr); a missing git or a timeout is returncode -1."""
    try:
        done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, check=False, input=stdin_text)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, "", str(e)
    return done.returncode, done.stdout, done.stderr


def _empty_status() -> dict[str, Any]:
    """The status shape before any line is parsed."""
    return {"branch": None, "detached": False, "head": None, "upstream": None,
            "ahead": 0, "behind": 0, "staged": [], "unstaged": [], "untracked": []}


def _header(out: dict[str, Any], line: str) -> None:
    """Apply one ``# branch.*`` header."""
    key, _, value = line[2:].partition(" ")
    if key == "branch.oid":
        out["head"] = None if value == "(initial)" else value
    elif key == "branch.head":
        out["detached"] = value == "(detached)"
        out["branch"] = None if out["detached"] else value
    elif key == "branch.upstream":
        out["upstream"] = value
    elif key == "branch.ab":
        parts = value.split()
        if len(parts) == 2:
            out["ahead"], out["behind"] = abs(int(parts[0])), abs(int(parts[1]))


def _change(out: dict[str, Any], xy: str, path: str, orig: str | None) -> None:
    """File a changed entry under staged and/or unstaged by its XY code."""
    x, y = xy[0], xy[1]
    if x != ".":
        row: dict[str, Any] = {"path": path, "status": x}
        if orig is not None:
            row["orig"] = orig
        out["staged"].append(row)
    if y != ".":
        out["unstaged"].append({"path": path, "status": y})


def parse_status(raw: str) -> dict[str, Any]:
    """Parse ``git status --porcelain=v2 --branch -z`` output."""
    out = _empty_status()
    fields = raw.split("\0")
    i = 0
    for _ in range(min(len(fields), _MAX_ENTRIES * 2)):
        if i >= len(fields):
            break
        line = fields[i]
        i += 1
        if not line:
            continue
        kind = line[0]
        if kind == "#":
            _header(out, line)
        elif kind == "1":
            parts = line.split(" ", 8)
            _change(out, parts[1], parts[8], None)
        elif kind == "2":
            parts = line.split(" ", 9)
            orig = fields[i] if i < len(fields) else None
            i += 1
            _change(out, parts[1], parts[9], orig)
        elif kind == "u":
            out["unstaged"].append({"path": line.split(" ", 10)[10], "status": "U"})
        elif kind == "?":
            out["untracked"].append({"path": line[2:], "status": "?"})
    return out


def status(cwd: str) -> dict[str, Any]:
    """Branch, ahead/behind and the three file lists for the repo at ``cwd``."""
    code, stdout, stderr = run_git(_STATUS_ARGV, cwd)
    if code != 0:
        return {"error": (stderr.strip() or "git status failed")[:500]}
    try:
        return parse_status(stdout)
    except (IndexError, ValueError) as e:
        return {"error": f"unparseable git status: {e}"}


def _worktree_row(block: list[str]) -> dict[str, Any] | None:
    """One ``git worktree list --porcelain`` stanza as a row."""
    row: dict[str, Any] = {"path": None, "branch": None, "head": None,
                           "detached": False, "bare": False, "locked": False}
    for line in block:
        key, _, value = line.partition(" ")
        if key == "worktree":
            row["path"] = value
        elif key == "HEAD":
            row["head"] = value
        elif key == "branch":
            row["branch"] = value[len("refs/heads/"):] if value.startswith("refs/heads/") else value
        elif key in ("detached", "bare", "locked"):
            row[key] = True
    return row if row["path"] else None


def parse_worktrees(raw: str) -> list[dict[str, Any]]:
    """Parse ``git worktree list --porcelain``; the first row is the main tree."""
    rows: list[dict[str, Any]] = []
    for stanza in raw.strip().split("\n\n")[:_MAX_ENTRIES]:
        row = _worktree_row([ln for ln in stanza.splitlines() if ln])
        if row is not None:
            row["is_main"] = not rows
            rows.append(row)
    return rows


def worktrees(cwd: str) -> dict[str, Any]:
    """``{"worktrees": [...]}`` for the repo at ``cwd``, or ``{"error"}``."""
    code, stdout, stderr = run_git(["worktree", "list", "--porcelain"], cwd)
    if code != 0:
        return {"error": (stderr.strip() or "git worktree list failed")[:500]}
    return {"worktrees": parse_worktrees(stdout)}
