"""ShadowRepo checkpoints, diffs and restores a work tree without touching its .git."""
from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

from coding_harness.core import git as shadow_git
from coding_harness.core.git import DIFF_CAP, ShadowRepo
from coding_harness.core.paths import meta_dir


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A work tree with three tracked-looking files and a .gitignore."""
    (tmp_path / "keep.txt").write_text("keep\n")
    (tmp_path / "edit.txt").write_text("before\n")
    (tmp_path / "gone.txt").write_text("doomed\n")
    (tmp_path / ".gitignore").write_text("secret.txt\n")
    return tmp_path


def _repo(cwd: Path) -> ShadowRepo:
    """A shadow bound to ``cwd`` under a fresh session id."""
    return ShadowRepo(f"test-{uuid.uuid4().hex[:8]}", str(cwd))


def _mutate(tree: Path) -> None:
    """Modify one file, add one, delete one."""
    (tree / "edit.txt").write_text("after\n")
    (tree / "new.txt").write_text("fresh\n")
    (tree / "gone.txt").unlink()


def test_git_dir_lives_under_meta_dir(tree: Path) -> None:
    repo = _repo(tree)
    assert repo.checkpoint("c0")
    assert repo.git_dir.resolve().is_relative_to(meta_dir().resolve())
    assert (repo.git_dir / "HEAD").exists()


def test_diff_files_reports_added_modified_deleted(tree: Path) -> None:
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    _mutate(tree)
    by_path = {f["path"]: f for f in repo.diff_files(sha)}
    assert {p: f["status"] for p, f in by_path.items()} == {
        "edit.txt": "modified", "new.txt": "added", "gone.txt": "deleted",
    }
    assert "+after" in by_path["edit.txt"]["diff"]
    assert not any(f["truncated"] for f in by_path.values())
    assert "+after" in repo.diff(sha)


def test_diff_files_truncates_at_cap(tree: Path) -> None:
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    (tree / "big.txt").write_text("x\n" * DIFF_CAP)
    (big,) = [f for f in repo.diff_files(sha) if f["path"] == "big.txt"]
    assert big["truncated"] and len(big["diff"]) == DIFF_CAP


def test_restore_returns_tree_to_checkpoint(tree: Path) -> None:
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    _mutate(tree)
    report = repo.restore(sha)
    assert report.errors == []
    assert report.removed == ["new.txt"]
    assert sorted(report.restored) == ["edit.txt", "gone.txt"]
    assert (tree / "edit.txt").read_text() == "before\n"
    assert (tree / "gone.txt").read_text() == "doomed\n"
    assert not (tree / "new.txt").exists()
    assert repo.diff_files(sha) == []


def test_restore_file_reverts_only_one_path(tree: Path) -> None:
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    _mutate(tree)
    assert repo.restore_file(sha, "edit.txt")
    assert (tree / "edit.txt").read_text() == "before\n"
    assert {f["path"] for f in repo.diff_files(sha)} == {"new.txt", "gone.txt"}
    assert repo.restore_file(sha, "new.txt")
    assert not (tree / "new.txt").exists()
    assert repo.restore_file(sha, "gone.txt")
    assert repo.diff_files(sha) == []


def test_restore_file_refuses_paths_outside_cwd(tree: Path) -> None:
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    assert not repo.restore_file(sha, "../elsewhere.txt")
    assert not repo.restore_file(sha, ".git/config")


def test_bad_sha_is_rejected_without_calling_git(tree: Path) -> None:
    repo = _repo(tree)
    repo.checkpoint("c0")
    assert repo.diff_files("--output=/tmp/x") == []
    assert repo.restore("not-a-sha").errors


def test_ignored_file_is_not_captured(tree: Path) -> None:
    repo = _repo(tree)
    (tree / "secret.txt").write_text("hunter2\n")
    sha = repo.checkpoint("c0")
    tracked = repo._git("ls-tree", "-r", "--name-only", sha) or ""
    assert "secret.txt" not in tracked.split()
    assert "keep.txt" in tracked.split()
    (tree / "secret.txt").write_text("changed\n")
    assert repo.diff_files(sha) == []
    repo.restore(sha)
    assert (tree / "secret.txt").read_text() == "changed\n"


def test_target_repo_git_dir_is_untouched(tree: Path) -> None:
    subprocess.run(["git", "init", "-q", str(tree)], check=True)
    before = sorted(str(p) for p in (tree / ".git").rglob("*"))
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    _mutate(tree)
    repo.diff_files(sha)
    repo.restore(sha)
    assert sorted(str(p) for p in (tree / ".git").rglob("*")) == before


def test_kill_switch_makes_every_method_a_noop(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HARNESS_SHADOW", "0")
    repo = _repo(tree)
    assert not repo.available()
    assert repo.checkpoint("c0") is None
    assert repo.diff("abcd") == ""
    assert repo.diff_files("abcd") == []
    assert not repo.restore_file("abcd", "edit.txt")
    report = repo.restore("abcd")
    assert report.restored == [] and report.removed == []
    assert not repo.git_dir.exists()
    assert (tree / "edit.txt").read_text() == "before\n"


def test_missing_git_fails_open(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shadow_git.shutil, "which", lambda _name: None)
    repo = _repo(tree)
    assert not repo.available()
    assert repo.checkpoint("c0") is None
    assert repo.diff_files("abcd") == []
    assert repo.restore("abcd").removed == []


def test_cwd_not_a_directory_is_unavailable(tree: Path) -> None:
    assert not _repo(tree / "keep.txt").available()
    assert not _repo(tree / "missing").available()


def test_uncreatable_shadow_dir_is_unavailable(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tree / "meta-is-a-file"
    blocker.write_text("")
    monkeypatch.setenv("HARNESS_META_DIR", str(blocker))
    repo = _repo(tree)
    assert not repo.available()
    assert repo.checkpoint("c0") is None
    assert os.path.isfile(blocker)


def test_diff_files_between_two_checkpoints_ignores_later_edits(tree: Path) -> None:
    repo = _repo(tree)
    start = repo.checkpoint("c0")
    _mutate(tree)
    end = repo.checkpoint("c1")
    (tree / "keep.txt").write_text("later\n")
    paths = sorted(f["path"] for f in repo.diff_files(start, end))
    assert paths == ["edit.txt", "gone.txt", "new.txt"]
    assert repo.diff_files(start, "--output=x") == []


def test_restore_file_never_deletes_an_ignored_file(tree: Path) -> None:
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    (tree / "secret.txt").write_text("token\n")
    assert not repo.restore_file(sha, "secret.txt")
    assert (tree / "secret.txt").read_text() == "token\n"


def test_restore_file_refuses_git_dir_in_any_case(tree: Path) -> None:
    (tree / ".git").mkdir()
    (tree / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    for name in (".git/HEAD", ".GIT/HEAD", ".Git/HEAD"):
        assert not repo.restore_file(sha, name)
    assert (tree / ".git" / "HEAD").exists()


def test_restore_file_bogus_path_is_false(tree: Path) -> None:
    repo = _repo(tree)
    sha = repo.checkpoint("c0")
    assert not repo.restore_file(sha, "no/such/file.txt")
