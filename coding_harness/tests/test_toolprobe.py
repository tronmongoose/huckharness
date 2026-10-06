"""Tests for the tool probe injected into the system prompt (P0-3)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from coding_harness.context import toolprobe
from coding_harness.context.toolprobe import probe, render
from coding_harness.modes.print_mode import build_system_prompt


@pytest.fixture(autouse=True)
def _fresh_cache():
    toolprobe._CACHE.clear()
    yield
    toolprobe._CACHE.clear()


def test_render_shape(tmp_path: Path):
    text = render(probe(str(tmp_path)))
    lines = text.strip().splitlines()
    assert lines[0] == "Available tooling (use these exact paths):"
    assert lines[1].startswith("  python: ")
    assert sys.version.split()[0] in lines[1]
    for line in lines[1:]:
        assert line.startswith("  ")


def test_python_prefers_local_venv(tmp_path: Path):
    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    os.symlink(sys.executable, venv_bin / "python")
    info = probe(str(tmp_path))
    assert info["tools"]["python"]["path"] == str(venv_bin / "python")
    assert info["tools"]["python"]["version"] == sys.version.split()[0]


def test_probe_is_cached_and_byte_stable(tmp_path: Path):
    first = probe(str(tmp_path))
    with mock.patch.object(toolprobe.subprocess, "run") as run:
        second = probe(str(tmp_path))
        assert run.call_count == 0
    assert first is second
    assert render(first) == render(second)


def test_kill_switch_returns_empty(tmp_path: Path):
    with mock.patch.dict(os.environ, {"HARNESS_TOOL_PROBE": "0"}):
        assert probe(str(tmp_path)) == {}
        assert render({"tools": {"make": {"path": "/usr/bin/make", "version": None}}}) == ""
    assert render({}) == ""


def test_missing_tool_rendered_as_not_available(tmp_path: Path):
    real_which = shutil.which

    def _which(name, *args, **kwargs):
        return None if name == "node" else real_which(name, *args, **kwargs)

    with mock.patch.object(toolprobe.shutil, "which", side_effect=_which):
        info = probe(str(tmp_path))
    assert "node" in info["missing"]
    assert "node" not in info["tools"]
    text = render(info)
    assert "  not available: " in text
    assert "node" in text.rsplit("not available: ", 1)[1]


def test_hanging_version_never_raises(tmp_path: Path):
    def _hang(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 2))

    with mock.patch.object(toolprobe.subprocess, "run", side_effect=_hang):
        info = probe(str(tmp_path))
    assert info["tools"] == {}
    assert info["missing"][0] == "python"
    text = render(info)
    assert text.strip().splitlines()[-1].startswith("  not available: python")


def test_system_prompt_carries_tooling_block_after_conventions(tmp_path: Path, monkeypatch):
    # HOME must be isolated: the prompt tail includes the skill index, which
    # reads user-level roots, so a real ~/.claude/skills would append to the
    # prompt and break endswith for reasons unrelated to tooling order.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / ".git").mkdir()
    (tmp_path / "AGENTS.md").write_text("CONVENTION-MARKER\n", encoding="utf-8")
    prompt = build_system_prompt(str(tmp_path), include_repo_map=False)
    block = render(probe(str(tmp_path)))
    assert block.startswith("\n\nAvailable tooling")
    assert prompt.endswith(block)
    assert prompt.index("CONVENTION-MARKER") < prompt.index("Available tooling")
    with mock.patch.dict(os.environ, {"HARNESS_TOOL_PROBE": "0"}):
        assert "Available tooling" not in build_system_prompt(str(tmp_path), include_repo_map=False)
