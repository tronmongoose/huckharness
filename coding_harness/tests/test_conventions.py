"""Convention-file ingestion: precedence, ordering, budget, git ceiling."""
from __future__ import annotations

from coding_harness.context.conventions import (
    find_convention_files,
    render_conventions,
)


def _git_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    return root


def test_prefers_agents_over_claude_same_dir(tmp_path):
    root = _git_repo(tmp_path)
    (root / "AGENTS.md").write_text("agent rules")
    (root / "CLAUDE.md").write_text("human rules")
    found = find_convention_files(root)
    assert [p.name for p in found] == ["AGENTS.md"]


def test_falls_back_to_claude_when_no_agents(tmp_path):
    root = _git_repo(tmp_path)
    (root / "CLAUDE.md").write_text("human rules")
    assert [p.name for p in find_convention_files(root)] == ["CLAUDE.md"]


def test_root_down_order_closer_last(tmp_path):
    root = _git_repo(tmp_path)
    (root / "AGENTS.md").write_text("root")
    sub = root / "pkg"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("closer")
    found = find_convention_files(sub)
    assert [p.parent.name for p in found] == [root.name, "pkg"]
    block = render_conventions(sub)
    assert block.index("root") < block.index("closer")


def test_walk_stops_at_git_ceiling(tmp_path):
    outer = tmp_path / "outer"
    outer.mkdir()
    (outer / "AGENTS.md").write_text("outside repo")
    repo = _git_repo(outer / "repo")
    (repo / "AGENTS.md").write_text("inside repo")
    found = find_convention_files(repo)
    assert [p.parent.name for p in found] == ["repo"]  # never reaches outer


def test_oversized_file_skipped_whole_not_truncated(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _git_repo(tmp_path / "repo")
    big = "x" * 20_000
    (root / "CLAUDE.md").write_text(big)
    block = render_conventions(root, budget_bytes=500)
    assert big not in block  # not injected
    assert "exceed the injection budget" in block  # pointed at, not partial


def test_empty_when_no_conventions(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert render_conventions(_git_repo(tmp_path / "repo")) == ""


def test_global_layer_renders_first(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".config" / "bjorn").mkdir(parents=True)
    (home / ".config" / "bjorn" / "AGENTS.md").write_text("global rules")
    monkeypatch.setenv("HOME", str(home))
    root = _git_repo(tmp_path / "repo")
    monkeypatch.delenv("HARNESS_MEMORY_DIR", raising=False)
    (root / "AGENTS.md").write_text("repo rules")
    block = render_conventions(root)
    assert block.index("global rules") < block.index("repo rules")


def test_memory_index_injected_after_repo_layer(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    root = _git_repo(tmp_path / "repo")
    monkeypatch.delenv("HARNESS_MEMORY_DIR", raising=False)
    (root / "AGENTS.md").write_text("repo rules")
    mem = home / ".config" / "bjorn" / "memory" / "repo"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- remembered thing")
    block = render_conventions(root)
    assert "## Memory" in block
    assert block.index("repo rules") < block.index("- remembered thing")


def test_memory_alone_still_renders(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    root = _git_repo(tmp_path / "repo")
    monkeypatch.delenv("HARNESS_MEMORY_DIR", raising=False)
    mem = home / ".config" / "bjorn" / "memory" / "repo"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("- only memory")
    block = render_conventions(root)
    assert "## Memory" in block and "- only memory" in block
    assert "Repository conventions" not in block
