"""Tests for the bash_rewind CLI."""
from __future__ import annotations

from pathlib import Path

import pytest

from coding_harness.security.bash_rewind import main
from coding_harness.security.bash_snapshot import snapshot_bash


@pytest.fixture
def populated_snap(tmp_path: Path) -> tuple[Path, Path]:
    """Create a real bash snapshot under tmp_path and return (root, snap_dir)."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "a.txt").write_text("alpha")

    root = tmp_path / "snaps"
    snap_dir, captured = snapshot_bash(
        "echo new > a.txt",
        cwd=workdir,
        session_id="sX",
        root=root,
    )
    assert snap_dir is not None and captured
    # Simulate the destructive write actually happening so restore is meaningful.
    (workdir / "a.txt").write_text("MUTATED")
    return root, snap_dir


def test_list_empty(tmp_path, capsys):
    rc = main(["list", "--root", str(tmp_path / "empty")])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No bash snapshots" in out


def test_list_with_session_filter(populated_snap, capsys):
    root, snap_dir = populated_snap
    rc = main(["list", "--root", str(root), "--session", "sX"])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(snap_dir) in out
    assert "session=sX" in out


def test_list_session_filter_misses(populated_snap, capsys):
    root, _ = populated_snap
    rc = main(["list", "--root", str(root), "--session", "no-such-session"])
    assert rc == 0
    assert "No bash snapshots" in capsys.readouterr().out


def test_show_includes_diff(populated_snap, capsys):
    _, snap_dir = populated_snap
    rc = main(["show", str(snap_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "alpha" in out  # pre-image content
    assert "MUTATED" in out  # current content
    assert "diff:" in out


def test_show_missing_manifest(tmp_path, capsys):
    rc = main(["show", str(tmp_path / "no-such-dir")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "no manifest" in err


def test_restore_yes_writes_pre_image_back(populated_snap, capsys):
    root, snap_dir = populated_snap
    # find the workdir from the manifest
    import json
    manifest = json.loads((snap_dir / "manifest.json").read_text())
    target_path = Path(manifest["targets"][0]["path"])
    assert target_path.read_text() == "MUTATED"

    rc = main(["restore", str(snap_dir), "--yes"])
    assert rc == 0
    assert target_path.read_text() == "alpha"
    out = capsys.readouterr().out
    assert "Restored 1" in out


def test_restore_reports_missing_pre_image(populated_snap, capsys):
    _, snap_dir = populated_snap
    # Delete the pre-image to simulate corruption
    for pre in snap_dir.glob("*.pre"):
        pre.unlink()
    rc = main(["restore", str(snap_dir), "--yes"])
    assert rc == 1
    out = capsys.readouterr().out
    assert "failed 1" in out
    assert "pre-image missing" in out
