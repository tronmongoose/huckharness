"""Custom slash commands: resolution order and $ARGUMENTS substitution."""
from __future__ import annotations

from coding_harness.modes import custom_commands


def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def _project(tmp_path):
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    return project


def test_expand_substitutes_arguments(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = _project(tmp_path)
    cmd_dir = project / ".bjorn" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "fix.md").write_text("Fix the bug: $ARGUMENTS\nRun tests.\n")
    out = custom_commands.expand("/fix login crashes", cwd=str(project))
    assert out == "Fix the bug: login crashes\nRun tests.\n"


def test_expand_without_args_substitutes_empty(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = _project(tmp_path)
    cmd_dir = project / ".bjorn" / "commands"
    cmd_dir.mkdir(parents=True)
    (cmd_dir / "tidy.md").write_text("Tidy: [$ARGUMENTS]")
    assert custom_commands.expand("/tidy", cwd=str(project)) == "Tidy: []"


def test_project_wins_over_user(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    project = _project(tmp_path)
    user_dir = home / ".config" / "bjorn" / "commands"
    user_dir.mkdir(parents=True)
    (user_dir / "fix.md").write_text("user template")
    proj_dir = project / ".bjorn" / "commands"
    proj_dir.mkdir(parents=True)
    (proj_dir / "fix.md").write_text("project template")
    assert custom_commands.expand("/fix", cwd=str(project)) == "project template"


def test_user_fallback_when_no_project_file(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    project = _project(tmp_path)
    user_dir = home / ".config" / "bjorn" / "commands"
    user_dir.mkdir(parents=True)
    (user_dir / "note.md").write_text("note: $ARGUMENTS")
    assert custom_commands.expand("/note hi", cwd=str(project)) == "note: hi"


def test_non_commands_return_none(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = _project(tmp_path)
    assert custom_commands.expand("plain prompt", cwd=str(project)) is None
    assert custom_commands.expand("/missing", cwd=str(project)) is None
    assert custom_commands.expand("/../evil", cwd=str(project)) is None
    assert custom_commands.expand("/", cwd=str(project)) is None
