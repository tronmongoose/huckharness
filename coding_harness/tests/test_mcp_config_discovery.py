"""`.mcp.json` is discovered from the working directory, not the package."""
from __future__ import annotations

import json

from coding_harness.mcp import config


def test_find_config_walks_up_from_the_working_directory(tmp_path, monkeypatch):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert config.find_config() == (tmp_path / ".mcp.json").resolve()


def test_find_config_ignores_the_package_location(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    found = config.find_config()
    assert found is None or tmp_path.resolve() in found.resolve().parents or found.parent == tmp_path.resolve()


def test_explicit_start_still_wins(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    monkeypatch.chdir(tmp_path)
    assert config.find_config(project) == (project / ".mcp.json").resolve()
