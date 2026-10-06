"""Per-repo memory: slug resolution, index rendering, envelope write grants."""
from __future__ import annotations

import os

import pytest

from coding_harness.context.memory import (
    memory_dir,
    memory_root,
    render_memory,
    repo_slug,
)
from coding_harness.core.envelope import preset
from coding_harness.core.mode import Autonomy


@pytest.fixture(autouse=True)
def _home_default(monkeypatch):
    """These tests pin the HOME-derived default, so the suite's override is lifted."""
    monkeypatch.delenv("HARNESS_MEMORY_DIR", raising=False)


def _repo(tmp_path, name="myrepo"):
    root = tmp_path / name
    (root / ".git").mkdir(parents=True)
    return root


def test_repo_slug_is_git_root_basename(tmp_path):
    root = _repo(tmp_path)
    sub = root / "pkg" / "inner"
    sub.mkdir(parents=True)
    assert repo_slug(sub) == "myrepo"


def test_repo_slug_falls_back_to_cwd_basename(tmp_path):
    plain = tmp_path / "loose"
    plain.mkdir()
    assert repo_slug(plain) == "loose"


def test_memory_dir_under_config_bjorn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path)
    expected = tmp_path / "home" / ".config" / "bjorn" / "memory" / "myrepo"
    assert memory_dir(root) == expected


def test_memory_dir_env_override_wins_over_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(tmp_path / "mem"))
    assert memory_root() == tmp_path / "mem"
    assert memory_dir(_repo(tmp_path)) == tmp_path / "mem" / "myrepo"


def test_memory_root_expands_a_tilde_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_MEMORY_DIR", "~/elsewhere")
    assert memory_root() == tmp_path / "home" / "elsewhere"


def test_render_memory_absent_is_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert render_memory(_repo(tmp_path), 6000) == ""


def test_render_memory_returns_index_text(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path)
    mem = memory_dir(root)
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- one\n- two\n")
    assert render_memory(root, 6000) == "- one\n- two"


def test_render_memory_truncates_on_a_line_boundary(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path)
    mem = memory_dir(root)
    mem.mkdir(parents=True)
    lines = "\n".join(f"- entry {i:04d}" for i in range(100))
    (mem / "MEMORY.md").write_text(lines)
    out = render_memory(root, 100)
    assert 0 < len(out.encode()) <= 100
    assert all(line.startswith("- entry") for line in out.splitlines())
    assert not out.endswith("- ent")  # no half entry


def test_preset_grants_write_under_memory_dir_from_low_up(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path)
    target = str(memory_dir(root) / "MEMORY.md")
    for level in (Autonomy.LOW, Autonomy.MEDIUM, Autonomy.HIGH):
        env = preset(str(root), level)
        assert env.check("Write", {"file_path": target}).allowed, level
        assert env.check("Edit", {"file_path": target}).allowed, level


def test_preset_off_has_no_memory_write(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path)
    env = preset(str(root), Autonomy.OFF)
    target = str(memory_dir(root) / "MEMORY.md")
    assert not env.check("Write", {"file_path": target}).allowed


def test_preset_memory_grant_does_not_widen_reads_elsewhere(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _repo(tmp_path)
    env = preset(str(root), Autonomy.LOW)
    outside = str(tmp_path / "home" / ".config" / "bjorn" / "settings.json")
    assert not env.check("Write", {"file_path": outside}).allowed
    # cwd writes still allowed
    assert env.check("Write", {"file_path": str(root / "a.py")}).allowed
    assert os.path.realpath(str(root)) != os.path.realpath(str(memory_dir(root)))


def test_system_prompt_names_memory_dir_and_file_convention(tmp_path, monkeypatch):
    from coding_harness.modes.print_mode import build_system_prompt

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_TOOL_PROBE", "0")
    root = _repo(tmp_path)
    prompt = build_system_prompt(str(root), include_repo_map=False)
    expected = tmp_path / "home" / ".config" / "bjorn" / "memory" / "myrepo"
    assert f"lives in {expected}." in prompt
    assert "<type>_<slug>.md" in prompt
    assert "feedback, project,\nreference or user" in prompt
    for key in ("`name`", "`description`", "`metadata.type`"):
        assert key in prompt
    assert "pointer to MEMORY.md" in prompt
    assert prompt.index("Persistent memory for this repository") \
        < prompt.index("Do not announce future tool calls")
