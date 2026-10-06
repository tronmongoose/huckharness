"""Tests for the gray-zone Bash snapshot module."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_harness.security.bash_snapshot import (
    extract_targets,
    snapshot_bash,
)

# ── extract_targets ─────────────────────────────────────────────────────────


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.log").write_text("beta")
    (tmp_path / "c bin").write_bytes(b"\x00\x01\x02")
    return tmp_path


def test_redirect_overwrite(workdir: Path):
    out = extract_targets("echo hi > a.txt", cwd=workdir)
    assert out == [workdir / "a.txt"]


def test_redirect_append(workdir: Path):
    out = extract_targets("echo hi >> b.log", cwd=workdir)
    assert out == [workdir / "b.log"]


def test_double_redirect_not_treated_as_overwrite(workdir: Path):
    """`>>` is matched by the append regex, not the overwrite regex."""
    out = extract_targets("echo hi >> b.log", cwd=workdir)
    assert out == [workdir / "b.log"]
    # And the overwrite path doesn't double-fire
    assert len(out) == 1


def test_sed_inplace(workdir: Path):
    out = extract_targets("sed -i 's/x/y/g' a.txt", cwd=workdir)
    assert workdir / "a.txt" in out


def test_rm_single_file(workdir: Path):
    out = extract_targets("rm a.txt", cwd=workdir)
    assert out == [workdir / "a.txt"]


def test_rm_recursive_skipped(workdir: Path):
    """Recursive rm is hard-blocked elsewhere; the extractor returns nothing."""
    assert extract_targets("rm -rf a.txt", cwd=workdir) == []
    assert extract_targets("rm -r a.txt", cwd=workdir) == []


def test_mv_destination(workdir: Path):
    out = extract_targets("mv a.txt b.log", cwd=workdir)
    assert out == [workdir / "b.log"]


def test_cp_destination(workdir: Path):
    out = extract_targets("cp a.txt b.log", cwd=workdir)
    assert out == [workdir / "b.log"]


def test_tee_overwrite(workdir: Path):
    out = extract_targets("echo hi | tee a.txt", cwd=workdir)
    assert out == [workdir / "a.txt"]


def test_nonexistent_target_excluded(workdir: Path):
    """Targets that don't exist as files have nothing to snapshot."""
    out = extract_targets("echo hi > does-not-exist.txt", cwd=workdir)
    assert out == []


def test_dedupe_repeated_targets(workdir: Path):
    out = extract_targets("sed -i 's/x/y/' a.txt && rm a.txt", cwd=workdir)
    assert out == [workdir / "a.txt"]


def test_embedded_null_target_is_skipped(workdir: Path):
    assert extract_targets("echo hi > 'a\x00.txt'; echo hi > a.txt", cwd=workdir) == [workdir / "a.txt"]


def test_unparseable_command_returns_empty(workdir: Path):
    """A shlex parse failure (unbalanced quote) doesn't crash."""
    out = extract_targets("rm 'unterminated", cwd=workdir)
    assert out == []


# ── snapshot_bash ───────────────────────────────────────────────────────────


def test_snapshot_captures_pre_image(workdir: Path, tmp_path: Path):
    snap_root = tmp_path / "snapshots"
    snap_dir, captured = snapshot_bash(
        "echo override > a.txt",
        cwd=workdir,
        session_id="s1",
        root=snap_root,
    )
    assert snap_dir is not None
    assert captured == [workdir / "a.txt"]
    pre_files = list(snap_dir.glob("*.pre"))
    assert len(pre_files) == 1
    assert pre_files[0].read_text() == "alpha"

    manifest = json.loads((snap_dir / "manifest.json").read_text())
    assert manifest["session_id"] == "s1"
    assert manifest["command"] == "echo override > a.txt"
    assert manifest["targets"][0]["path"] == str(workdir / "a.txt")


def test_snapshot_no_targets_returns_none(workdir: Path, tmp_path: Path):
    snap_dir, captured = snapshot_bash(
        "ls a.txt",  # read-only, nothing to extract
        cwd=workdir,
        session_id="s2",
        root=tmp_path / "snapshots",
    )
    assert snap_dir is None
    assert captured == []


def test_snapshot_handles_collision(workdir: Path, tmp_path: Path):
    """Two same-basename targets get disambiguated."""
    nested = workdir / "nested"
    nested.mkdir()
    (nested / "a.txt").write_text("nested-alpha")

    snap_root = tmp_path / "snapshots"
    snap_dir, captured = snapshot_bash(
        "sed -i 's/x/y/' a.txt && sed -i 's/x/y/' nested/a.txt",
        cwd=workdir,
        session_id="s3",
        root=snap_root,
    )
    assert snap_dir is not None
    assert len(captured) == 2
    pre_files = sorted(p.name for p in snap_dir.glob("*.pre"))
    assert pre_files == ["a.txt.1.pre", "a.txt.pre"]


def test_snapshot_skips_oversize(monkeypatch, workdir: Path, tmp_path: Path):
    """Files over MAX_PREIMAGE_BYTES are recorded as skipped, not captured."""
    import coding_harness.security.bash_snapshot as bs
    monkeypatch.setattr(bs, "MAX_PREIMAGE_BYTES", 2)
    snap_dir, captured = snapshot_bash(
        "echo hi > a.txt",
        cwd=workdir,
        session_id="s4",
        root=tmp_path / "snapshots",
    )
    # 'alpha' is 5 bytes > cap of 2 → skipped
    assert captured == []
    assert snap_dir is not None
    manifest = json.loads((snap_dir / "manifest.json").read_text())
    assert manifest["targets"][0]["skipped"] == "too_large"
