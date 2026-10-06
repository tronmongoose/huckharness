"""Custom commands and /skill expand to a model prompt in the REPL loop."""
from __future__ import annotations

from coding_harness.modes import repl_mode


def test_custom_command_expands(monkeypatch):
    monkeypatch.setattr(repl_mode.custom_commands, "expand",
                        lambda line, cwd=None: "expanded body" if line.startswith("/ship") else None)
    assert repl_mode._expand_command("/ship now") == "expanded body"
    assert repl_mode._expand_command("plain prompt") is None


def test_skill_expands_and_reports_unknown(monkeypatch):
    monkeypatch.setattr(repl_mode, "load_skill",
                        lambda name, cwd=None: "SKILL TEXT" if name == "deploy" else None)
    monkeypatch.setattr(repl_mode.custom_commands, "expand", lambda line, cwd=None: None)
    assert repl_mode._expand_command("/skill deploy") == "SKILL TEXT"
    assert repl_mode._expand_command("/skill nope") is repl_mode._UNKNOWN_SKILL
