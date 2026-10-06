"""HARNESS_PROMPT_OVERLAY renders an operating-notes block after the base prompt."""
from __future__ import annotations

from coding_harness.context import overlay
from coding_harness.modes import print_mode


def test_unset_renders_nothing(monkeypatch):
    monkeypatch.delenv(overlay.OVERLAY_ENV, raising=False)
    assert overlay.render_overlay() == ""


def test_missing_or_empty_file_renders_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv(overlay.OVERLAY_ENV, str(tmp_path / "absent.md"))
    assert overlay.render_overlay() == ""
    empty = tmp_path / "empty.md"
    empty.write_text("  \n")
    monkeypatch.setenv(overlay.OVERLAY_ENV, str(empty))
    assert overlay.render_overlay() == ""


def test_file_text_is_wrapped_and_capped(monkeypatch, tmp_path):
    f = tmp_path / "notes.md"
    f.write_text("Run the tests before finishing.\n")
    monkeypatch.setenv(overlay.OVERLAY_ENV, str(f))
    block = overlay.render_overlay()
    assert block.startswith("\n\n## Operating notes\n\n")
    assert "Run the tests before finishing." in block
    f.write_text("x" * (overlay.OVERLAY_CAP_BYTES + 500))
    assert len(overlay.render_overlay().encode()) <= overlay.OVERLAY_CAP_BYTES + 40


def test_overlay_sits_between_base_prompt_and_conventions(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_TOOL_PROBE", "0")
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "notes.md"
    f.write_text("OVERLAY-MARKER")
    monkeypatch.setenv(overlay.OVERLAY_ENV, str(f))
    prompt = print_mode.build_system_prompt(cwd=str(tmp_path), include_repo_map=False)
    marker = prompt.index("OVERLAY-MARKER")
    assert prompt.index("Do not announce future tool calls") < marker
    conventions = prompt.find("## Repository conventions")
    assert conventions == -1 or marker < conventions
