"""Lifecycle hooks: contract, matchers, fail modes, and registry wiring."""
from __future__ import annotations

import json
from types import SimpleNamespace

from coding_harness.core import hooks as hooks_mod
from coding_harness.core.hooks import HookRunner, build_runner
from coding_harness.security import audit, sentinel
from coding_harness.tools.base import Tool, ToolResult
from coding_harness.tools.registry import ToolRegistry


def _runner(config, **kwargs):
    kwargs.setdefault("session_id", "s1")
    kwargs.setdefault("cwd", "/tmp")
    kwargs.setdefault("log", lambda kind, payload: None)
    return HookRunner(config, **kwargs)


def test_build_runner_kill_switch_and_empty(monkeypatch):
    settings = SimpleNamespace(hooks={"Stop": [{"command": "true"}]})
    monkeypatch.setenv("HARNESS_HOOKS", "0")
    assert build_runner(settings, session_id="s", cwd="/tmp") is None
    monkeypatch.delenv("HARNESS_HOOKS")
    assert build_runner(settings, session_id="s", cwd="/tmp") is not None
    assert build_runner(SimpleNamespace(hooks={}), session_id="s", cwd="/tmp") is None


def test_pretooluse_exit_zero_allows():
    runner = _runner({"PreToolUse": [{"command": "exit 0"}]})
    decision = runner.run("PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
    assert decision.allowed


def test_pretooluse_exit_two_blocks_with_stderr_reason():
    runner = _runner({"PreToolUse": [{"command": "echo nope >&2; exit 2"}]})
    decision = runner.run("PreToolUse", tool_name="Bash", tool_input={"command": "ls"})
    assert not decision.allowed
    assert decision.reason == "nope"


def test_pretooluse_json_stdout_block_decision():
    cmd = """echo '{"decision": "block", "reason": "policy says no"}'"""
    runner = _runner({"PreToolUse": [{"command": cmd}]})
    decision = runner.run("PreToolUse", tool_name="Read", tool_input={})
    assert not decision.allowed
    assert decision.reason == "policy says no"


def test_pretooluse_fails_closed_on_timeout_and_crash():
    runner = _runner({"PreToolUse": [{"command": "sleep 5", "timeout": 0.2}]})
    decision = runner.run("PreToolUse", tool_name="Bash", tool_input={})
    assert not decision.allowed
    assert "timed out" in decision.reason

    runner = _runner({"PreToolUse": [{"command": "echo boom >&2; exit 3"}]})
    decision = runner.run("PreToolUse", tool_name="Bash", tool_input={})
    assert not decision.allowed
    assert "exited 3" in decision.reason


def test_other_events_fail_open_and_log():
    logged = []
    runner = _runner(
        {"PostToolUse": [{"command": "exit 3"}]},
        log=lambda kind, payload: logged.append(kind),
    )
    decision = runner.run("PostToolUse", tool_name="Bash", tool_input={})
    assert decision.allowed
    assert "hook_error" in logged


def test_stop_exit_two_returns_stderr_as_message():
    runner = _runner({"Stop": [{"command": "echo 'run the tests' >&2; exit 2"}]})
    decision = runner.run("Stop")
    assert decision.allowed
    assert decision.stop_messages == ["run the tests"]


def test_glob_matcher_on_tool_name():
    runner = _runner({"PreToolUse": [{"matcher": "Ba*", "command": "exit 2"}]})
    assert not runner.run("PreToolUse", tool_name="Bash", tool_input={}).allowed
    assert runner.run("PreToolUse", tool_name="Read", tool_input={}).allowed


def test_tool_prefix_matcher_matches_command_string():
    runner = _runner({"PreToolUse": [{"matcher": "Bash(rm *)", "command": "exit 2"}]})
    blocked = runner.run(
        "PreToolUse", tool_name="Bash", tool_input={"command": "rm -rf /tmp/x"},
    )
    assert not blocked.allowed
    assert runner.run(
        "PreToolUse", tool_name="Bash", tool_input={"command": "ls"},
    ).allowed
    assert runner.run(
        "PreToolUse", tool_name="Read", tool_input={"command": "rm x"},
    ).allowed


def test_payload_shape_on_stdin(tmp_path):
    out = tmp_path / "payload.json"
    runner = _runner(
        {"PostToolUse": [{"command": f"cat > {out}"}]},
        session_id="sess-9", cwd=str(tmp_path),
    )
    runner.run(
        "PostToolUse", tool_name="Write",
        tool_input={"file_path": "/a"}, tool_output="done",
    )
    payload = json.loads(out.read_text())
    assert payload == {
        "event": "PostToolUse", "session_id": "sess-9", "cwd": str(tmp_path),
        "tool_name": "Write", "tool_input": {"file_path": "/a"},
        "tool_output": "done",
    }


def test_env_is_scrubbed(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_API_TOKEN", "sekrit")
    out = tmp_path / "env.txt"
    runner = _runner({"Stop": [{"command": f'printf "%s" "$SOME_API_TOKEN" > {out}'}]})
    runner.run("Stop")
    assert out.read_text() == ""


def test_nested_claude_code_form_is_accepted():
    config = {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "exit 2"}]},
    ]}
    runner = _runner(config)
    assert not runner.run("PreToolUse", tool_name="Bash", tool_input={}).allowed


def test_bad_entries_are_skipped_and_logged():
    logged = []
    runner = _runner(
        {"PreToolUse": ["nonsense", {"matcher": "*"}]},
        log=lambda kind, payload: logged.append(kind),
    )
    assert runner.run("PreToolUse", tool_name="Bash", tool_input={}).allowed
    assert logged.count("hook_config_error") == 2


class _Echo(Tool):
    name = "Read"
    description = "echo"
    parameters = {"type": "object", "properties": {}}

    def run(self, args):  # type: ignore[no-untyped-def]
        return ToolResult(content="tool ran")


def _allowing_sentinel(monkeypatch):
    monkeypatch.setattr(
        sentinel, "review",
        lambda **kwargs: SimpleNamespace(allowed=True, reason="ok", path="test"),
    )
    monkeypatch.setattr(audit, "append", lambda **kwargs: {})


def test_registry_pretooluse_denies_the_call(monkeypatch):
    _allowing_sentinel(monkeypatch)
    registry = ToolRegistry()
    registry.register(_Echo())
    registry.hooks = _runner({"PreToolUse": [{"command": "echo no >&2; exit 2"}]})
    result = registry.dispatch("Read", {})
    assert result.is_error
    assert "BLOCKED by hook: no" in result.content


def test_registry_posttooluse_sees_the_output(monkeypatch, tmp_path):
    _allowing_sentinel(monkeypatch)
    out = tmp_path / "post.json"
    registry = ToolRegistry()
    registry.register(_Echo())
    registry.hooks = _runner({"PostToolUse": [{"command": f"cat > {out}"}]})
    result = registry.dispatch("Read", {})
    assert not result.is_error
    payload = json.loads(out.read_text())
    assert payload["tool_name"] == "Read"
    assert payload["tool_output"] == "tool ran"


def test_registry_without_hooks_is_untouched(monkeypatch):
    _allowing_sentinel(monkeypatch)
    registry = ToolRegistry()
    registry.register(_Echo())
    assert registry.hooks is None
    assert registry.dispatch("Read", {}).content == "tool ran"


def test_events_constant_lists_the_supported_events():
    assert hooks_mod.EVENTS == (
        "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
        "PreCompact", "Stop",
    )
