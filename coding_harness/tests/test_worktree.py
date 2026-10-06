"""bjorn --worktree: sibling worktree creation, branch reuse, refusals."""
from __future__ import annotations

import os
import subprocess

import pytest

from coding_harness import cli
from coding_harness.modes import repl_mode, worktree


def _git(args, cwd):
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    )


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    _git(["init", "-q", "-b", "main"], root)
    (root / "a.txt").write_text("a")
    _git(["add", "a.txt"], root)
    _git(["commit", "-q", "-m", "init"], root)
    monkeypatch.chdir(root)
    return root


def test_enter_creates_worktree_and_chdirs(repo, tmp_path):
    path = worktree.enter("topic", str(repo))
    assert path == tmp_path / "proj-topic"
    assert path.is_dir() and (path / "a.txt").exists()
    assert os.path.realpath(os.getcwd()) == os.path.realpath(str(path))
    branches = subprocess.run(
        ["git", "branch", "--list", "topic"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "topic" in branches


def test_enter_reuses_existing_branch(repo, tmp_path):
    _git(["branch", "existing"], repo)
    path = worktree.enter("existing", str(repo))
    assert path.is_dir()


def test_enter_refuses_existing_path(repo, tmp_path):
    (tmp_path / "proj-taken").mkdir()
    with pytest.raises(worktree.WorktreeError, match="already exists"):
        worktree.enter("taken", str(repo))


def test_enter_refuses_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(worktree.WorktreeError, match="not a git repository"):
        worktree.enter("t", str(plain))


def test_cli_worktree_starts_repl_in_the_new_path(repo, tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run(**kw):
        seen["cwd"] = os.getcwd()
        return 0

    monkeypatch.setattr(repl_mode, "run", fake_run)
    monkeypatch.setenv("HARNESS_SETTINGS", "off")
    rc = cli.main(["--worktree", "feat"])
    assert rc == 0
    expected = os.path.realpath(str(tmp_path / "proj-feat"))
    assert os.path.realpath(seen["cwd"]) == expected
    err = capsys.readouterr().err
    assert "worktree:" in err and "proj-feat" in err


def test_cli_worktree_rejects_a_prompt(repo, monkeypatch, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--worktree", "x", "do things"])
    assert "do not pass a prompt" in capsys.readouterr().err
