"""Porcelain v2 status and worktree-list parsing in core/gitinfo."""
from __future__ import annotations

import subprocess

from coding_harness.core import gitinfo

_OID = "a" * 40


def _z(*fields: str) -> str:
    """NUL-terminated porcelain -z output."""
    return "".join(f + "\0" for f in fields)


def test_clean_branch_without_upstream():
    got = gitinfo.parse_status(_z(f"# branch.oid {_OID}", "# branch.head main"))
    assert got["branch"] == "main" and not got["detached"] and got["head"] == _OID
    assert got["upstream"] is None and got["ahead"] == 0 and got["behind"] == 0
    assert got["staged"] == got["unstaged"] == got["untracked"] == []


def test_ahead_behind_with_upstream():
    got = gitinfo.parse_status(_z(f"# branch.oid {_OID}", "# branch.head topic",
                                  "# branch.upstream origin/topic", "# branch.ab +3 -2"))
    assert got["upstream"] == "origin/topic" and got["ahead"] == 3 and got["behind"] == 2


def test_detached_head_and_initial_commit():
    got = gitinfo.parse_status(_z(f"# branch.oid {_OID}", "# branch.head (detached)"))
    assert got["detached"] and got["branch"] is None
    fresh = gitinfo.parse_status(_z("# branch.oid (initial)", "# branch.head main"))
    assert fresh["head"] is None and fresh["branch"] == "main"


def test_changes_split_staged_unstaged_untracked():
    raw = _z(
        "# branch.head main",
        f"1 M. N... 100644 100644 100644 {_OID} {_OID} staged only.py",
        f"1 .M N... 100644 100644 100644 {_OID} {_OID} dirty.py",
        f"1 MM N... 100644 100644 100644 {_OID} {_OID} both.py",
        f"2 R. N... 100644 100644 100644 {_OID} {_OID} R100 new name.py", "old.py",
        "? notes/todo.txt",
        f"u UU N... 100644 100644 100644 100644 {_OID} {_OID} {_OID} clash.py",
    )
    got = gitinfo.parse_status(raw)
    assert got["staged"] == [
        {"path": "staged only.py", "status": "M"},
        {"path": "both.py", "status": "M"},
        {"path": "new name.py", "status": "R", "orig": "old.py"},
    ]
    assert got["unstaged"] == [
        {"path": "dirty.py", "status": "M"},
        {"path": "both.py", "status": "M"},
        {"path": "clash.py", "status": "U"},
    ]
    assert got["untracked"] == [{"path": "notes/todo.txt", "status": "?"}]


def test_worktree_list_parsing():
    raw = (
        "worktree /r/repo\nHEAD " + _OID + "\nbranch refs/heads/main\n\n"
        "worktree /r/repo-topic\nHEAD " + _OID + "\nbranch refs/heads/topic\nlocked\n\n"
        "worktree /r/repo-probe\nHEAD " + _OID + "\ndetached\n"
    )
    rows = gitinfo.parse_worktrees(raw)
    assert [r["path"] for r in rows] == ["/r/repo", "/r/repo-topic", "/r/repo-probe"]
    assert rows[0]["is_main"] and not rows[1]["is_main"]
    assert rows[1]["branch"] == "topic" and rows[1]["locked"]
    assert rows[2]["detached"] and rows[2]["branch"] is None


def test_status_fails_soft_outside_a_repo(tmp_path):
    assert "error" in gitinfo.status(str(tmp_path))
    assert "error" in gitinfo.worktrees(str(tmp_path))


def test_status_on_a_real_repo(tmp_path):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "a.txt").write_text("x")
    got = gitinfo.status(str(tmp_path))
    assert got["branch"] == "main" and got["untracked"] == [{"path": "a.txt", "status": "?"}]
