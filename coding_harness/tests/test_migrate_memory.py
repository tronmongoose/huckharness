"""Claude-memory migration: copies without overwriting, dry-run lists only."""
from __future__ import annotations

from coding_harness.tools.migrate_claude_memory import main, migrate, repo_slug_for


def _seed(home):
    mem = home / ".claude" / "projects" / "-Users-dev-projects-alpha" / "memory"
    mem.mkdir(parents=True)
    (mem / "MEMORY.md").write_text("index")
    (mem / "note.md").write_text("a note")
    return mem


def test_repo_slug_for():
    assert repo_slug_for("-Users-dev-projects-bjorn-harness") == "bjorn-harness"
    assert repo_slug_for("-private-tmp") == "private-tmp"


def test_migrate_copies_and_counts(tmp_path):
    home = tmp_path
    _seed(home)
    dest = home / ".config" / "bjorn" / "memory"
    results = migrate(home / ".claude" / "projects", dest, dry_run=False)
    assert results == [("alpha", 2)]
    assert (dest / "alpha" / "MEMORY.md").read_text() == "index"
    assert (dest / "alpha" / "note.md").read_text() == "a note"


def test_migrate_never_overwrites(tmp_path):
    home = tmp_path
    _seed(home)
    dest = home / ".config" / "bjorn" / "memory"
    (dest / "alpha").mkdir(parents=True)
    (dest / "alpha" / "MEMORY.md").write_text("mine already")
    results = migrate(home / ".claude" / "projects", dest, dry_run=False)
    assert results == [("alpha", 1)]  # only note.md
    assert (dest / "alpha" / "MEMORY.md").read_text() == "mine already"


def test_dry_run_lists_and_copies_nothing(tmp_path, capsys):
    home = tmp_path
    _seed(home)
    dest = home / ".config" / "bjorn" / "memory"
    results = migrate(home / ".claude" / "projects", dest, dry_run=True)
    assert results == [("alpha", 2)]
    assert not dest.exists()
    assert "would copy" in capsys.readouterr().out


def test_main_uses_home_and_prints_counts(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HARNESS_MEMORY_DIR", raising=False)
    _seed(tmp_path)
    assert main([]) == 0
    out = capsys.readouterr().out
    assert "alpha: copied 2 file(s)" in out
    assert (tmp_path / ".config" / "bjorn" / "memory" / "alpha" / "MEMORY.md").exists()


def test_main_with_no_projects(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert main(["--dry-run"]) == 0
    assert "no memory directories" in capsys.readouterr().out
