"""The trust gate on a project's sentinel hook and ``.mcp.json``, and the MCP child environment."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest

from coding_harness.core import trust
from coding_harness.mcp.config import MCPServerConfig, load_mcp_config
from coding_harness.mcp.transport import StdioTransport
from coding_harness.security import hook_adapter

HOOK_SOURCE = (
    "import pathlib\n"
    "pathlib.Path(__file__).with_name('ran.marker').write_text('ran')\n"
    "raise SystemExit(0)\n"
)
MCP_JSON = {"mcpServers": {"evil": {"command": "sh", "args": ["-c", "id"]}}}
DUMP_ENV = "import json, os, sys; open(sys.argv[1], 'w').write(json.dumps(dict(os.environ)))"


@pytest.fixture
def base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real tmp root with the trust gate on, no hook override and an empty resolve cache."""
    root = Path(os.path.realpath(str(tmp_path)))
    monkeypatch.delenv("HARNESS_TRUST_PROJECTS", raising=False)
    monkeypatch.delenv("SENTINEL_GATE_HOOK", raising=False)
    monkeypatch.setattr(trust, "TRUST_FILE", root / "cfg" / "trusted_projects.json")
    monkeypatch.setattr(hook_adapter, "_CACHE", {})
    return root


def _with_hook(directory: Path) -> Path:
    """Give ``directory`` a marker-writing sentinel hook; returns the marker path."""
    hook = directory / hook_adapter.HOOK_REL
    hook.parent.mkdir(parents=True)
    hook.write_text(HOOK_SOURCE)
    return hook.with_name("ran.marker")


def _review(cwd: Path) -> hook_adapter.HookVerdict | None:
    return hook_adapter.review("Bash", "shell", {"command": "ls"}, "s1", cwd=str(cwd))


def test_untrusted_hook_is_not_resolved_or_executed(base: Path) -> None:
    proj = base / "clone"
    marker = _with_hook(proj)
    assert hook_adapter._walk_up(proj) is None
    assert hook_adapter.resolve(str(proj)) is None
    assert _review(proj) is None
    assert _review(proj / ".claude") is None
    assert not marker.exists()


def test_trusted_hook_is_resolved_and_executed(base: Path) -> None:
    proj = base / "clone"
    marker = _with_hook(proj)
    trust.trust(proj)
    assert hook_adapter.resolve(str(proj)) == proj / hook_adapter.HOOK_REL
    verdict = _review(proj)
    assert verdict is not None and verdict.allowed
    assert marker.read_text() == "ran"


def test_hook_found_from_a_trusted_subdirectory_of_an_untrusted_repo_is_skipped(base: Path) -> None:
    proj = base / "clone"
    marker = _with_hook(proj)
    sub = proj / "src"
    sub.mkdir()
    trust.trust(sub)
    assert _review(sub) is None
    assert not marker.exists()


def test_untrusted_nested_hook_does_not_hide_the_trusted_one_above(base: Path) -> None:
    outer = base / "deploy"
    outer_marker = _with_hook(outer)
    trust.trust(outer)
    lookalike = base / "deploy-evil"
    evil_marker = _with_hook(lookalike)
    assert _review(lookalike) is None
    assert hook_adapter.resolve(str(outer / "sub")) == outer / hook_adapter.HOOK_REL
    assert not evil_marker.exists() and not outer_marker.exists()


def test_untrusted_hook_reached_through_a_symlinked_cwd_is_skipped(base: Path) -> None:
    proj = base / "clone"
    marker = _with_hook(proj)
    trusted = base / "trusted"
    trusted.mkdir()
    trust.trust(trusted)
    (trusted / "link").symlink_to(proj, target_is_directory=True)
    assert _review(trusted / "link") is None
    assert not marker.exists()


def test_untrusted_verdict_is_not_served_from_a_cache_after_trust_env_changes(
    base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = base / "clone"
    marker = _with_hook(proj)
    monkeypatch.setenv("HARNESS_TRUST_PROJECTS", "all")
    assert hook_adapter.resolve(str(proj)) is not None
    monkeypatch.delenv("HARNESS_TRUST_PROJECTS")
    hook_adapter._CACHE.clear()
    assert _review(proj) is None
    assert not marker.exists()


def _with_mcp(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ".mcp.json"
    path.write_text(json.dumps(MCP_JSON))
    return path


def test_discovered_mcp_config_in_untrusted_directory_yields_nothing(
    base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = base / "clone"
    _with_mcp(proj)
    (proj / "src").mkdir()
    for cwd in (proj, proj / "src"):
        monkeypatch.chdir(cwd)
        assert load_mcp_config() == []


def test_untrusted_malformed_mcp_config_is_not_even_parsed(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = base / "clone"
    proj.mkdir()
    (proj / ".mcp.json").write_text("{not json")
    monkeypatch.chdir(proj)
    assert load_mcp_config() == []


def test_discovered_mcp_config_in_trusted_directory_loads(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = base / "clone"
    _with_mcp(proj)
    (proj / "src").mkdir()
    trust.trust(proj)
    monkeypatch.chdir(proj / "src")
    servers = load_mcp_config()
    assert [(s.name, s.command, s.args) for s in servers] == [("evil", "sh", ["-c", "id"])]


def test_trusted_cwd_below_an_untrusted_mcp_config_yields_nothing(
    base: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = base / "clone"
    _with_mcp(proj)
    sub = proj / "src"
    sub.mkdir()
    trust.trust(sub)
    monkeypatch.chdir(sub)
    assert load_mcp_config() == []


def test_explicit_mcp_path_is_not_gated(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = _with_mcp(base / "clone")
    monkeypatch.chdir(base)
    assert [s.name for s in load_mcp_config(path)] == ["evil"]


def _child_env(config: MCPServerConfig, out: Path) -> dict[str, str]:
    """Start a stdio server that dumps its environment to ``out``; returns that environment."""
    transport = StdioTransport(config)
    transport.start(lambda _msg: None, lambda _reason: None)
    try:
        for _ in range(100):
            if out.exists() and out.read_text():
                break
            time.sleep(0.05)
        return json.loads(out.read_text())
    finally:
        transport.close()


def test_stdio_child_env_drops_credentials_it_did_not_name(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO_TOKEN", "t0ken")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-x")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-x")
    monkeypatch.setenv("DB_PASSWORD", "pw")
    monkeypatch.setenv("NAMED_TOKEN", "from-parent")
    monkeypatch.setenv("PLAIN_SETTING", "kept")
    out = base / "env.json"
    config = MCPServerConfig(name="dump", command=sys.executable, args=["-c", DUMP_ENV, str(out)],
                             env={"NAMED_TOKEN": "named-by-server"})
    env = _child_env(config, out)
    for name in ("FOO_TOKEN", "ANTHROPIC_API_KEY", "AWS_SECRET_ACCESS_KEY", "GITHUB_TOKEN", "DB_PASSWORD"):
        assert name not in env, name
    assert env["NAMED_TOKEN"] == "named-by-server"
    assert env["PLAIN_SETTING"] == "kept"
    assert env["PATH"] == os.environ["PATH"]
    assert env["HOME"] == os.environ["HOME"]


def test_stdio_child_gets_a_credential_its_env_block_references(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOO_TOKEN", "t0ken")
    monkeypatch.setenv("OTHER_TOKEN", "not-named")
    out = base / "env.json"
    path = base / "explicit.json"
    path.write_text(json.dumps({"mcpServers": {"dump": {
        "command": sys.executable, "args": ["-c", DUMP_ENV, str(out)],
        "env": {"FOO_TOKEN": "${FOO_TOKEN}"}}}}))
    (config,) = load_mcp_config(path)
    env = _child_env(config, out)
    assert env["FOO_TOKEN"] == "t0ken"
    assert "OTHER_TOKEN" not in env
