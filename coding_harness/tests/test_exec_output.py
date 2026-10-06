"""The JSON result envelope and exit codes for headless print mode."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

from coding_harness import cli
from coding_harness.core.session import SessionResult
from coding_harness.core.settings import Settings
from coding_harness.modes import exec_output, print_mode


def _result(reason: str) -> SessionResult:
    return SessionResult(final_text="done", turns=2, session_id="s1",
                         session_log_path=Path("/tmp/s1.jsonl"), halted_reason=reason,
                         files_changed=["a.py"], checks_passed=True)


def test_envelope_carries_every_contract_field():
    body = exec_output.envelope(_result("model_done"), model="m", autonomy="off", duration_ms=7)
    assert set(body) == {"session_id", "halted_reason", "final_text", "turns", "tokens_in",
                         "tokens_out", "files_changed", "checks_passed", "duration_ms",
                         "error", "session_log", "autonomy", "model"}
    assert body["session_id"] == "s1" and body["files_changed"] == ["a.py"]
    assert body["final_text"] == "done" and body["halted_reason"] == "model_done"
    assert body["autonomy"] == "off" and body["model"] == "m" and body["duration_ms"] == 7
    assert json.loads(json.dumps(body)) == body


class _FakeSession:
    """Stands in for Session: returns a canned result, or waits to be interrupted."""
    reason = "deadline"
    wait_for_interrupt = False

    def __init__(self, **kw):
        self._stop = threading.Event()

    def interrupt(self):
        self._stop.set()

    def run(self, prompt, *, deadline_s=None):
        if self.wait_for_interrupt:
            self._stop.wait(5)
            return _result("interrupted")
        return _result(self.reason)

    # print_mode takes run_turn (not run) when hooks are configured. A double
    # that implements only half the interface passes until the branch flips.
    def run_turn(self, prompt, *, deadline_s=None):
        self.turns_run = getattr(self, "turns_run", 0) + 1
        return self.run(prompt, deadline_s=deadline_s)

    def close(self):
        self.closed = True


def _run_json(monkeypatch, capsys, tmp_path, **attrs):
    for k, v in attrs.items():
        monkeypatch.setattr(_FakeSession, k, v)
    monkeypatch.setattr(print_mode, "Session", _FakeSession)
    monkeypatch.setenv("HARNESS_TOOL_PROBE", "0")
    monkeypatch.chdir(tmp_path)
    rc = print_mode.run("hi", enable_mcp=False, output_format="json")
    out, err = capsys.readouterr()
    return rc, out, err


def test_json_mode_prints_one_envelope_and_maps_the_exit_code(monkeypatch, capsys, tmp_path):
    rc, out, err = _run_json(monkeypatch, capsys, tmp_path, reason="deadline")
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1 and json.loads(lines[0])["halted_reason"] == "deadline"
    assert json.loads(lines[0])["final_text"] == "done"
    assert rc == 3
    assert '"event": "summary"' not in err


def test_ctrl_c_becomes_an_interrupted_envelope(monkeypatch, capsys, tmp_path):
    import _thread

    monkeypatch.setattr(print_mode, "Session", _FakeSession)
    monkeypatch.setattr(_FakeSession, "wait_for_interrupt", True)
    monkeypatch.setenv("HARNESS_TOOL_PROBE", "0")
    monkeypatch.chdir(tmp_path)
    threading.Timer(0.3, _thread.interrupt_main).start()
    rc = print_mode.run("hi", enable_mcp=False, output_format="json")
    out, _ = capsys.readouterr()
    assert json.loads(out.strip())["halted_reason"] == "interrupted" and rc == 6


def test_exit_codes_follow_the_table():
    assert [exec_output.exit_code(r) for r in
            ("model_done", "deadline", "max_turns", "stuck", "error", "interrupted")] == [0, 3, 4, 4, 5, 6]
    assert exec_output.exit_code("never_heard_of") == 5


def test_cli_passes_output_format_to_print_mode(monkeypatch):
    seen = {}

    def fake_run(prompt, **kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(print_mode, "run", fake_run)
    monkeypatch.setattr(cli, "_settings", lambda: Settings())
    assert cli.main(["--output-format", "json", "--no-mcp", "hello"]) == 0
    assert seen["output_format"] == "json"


def test_cli_takes_the_model_from_settings_when_the_flag_is_absent(monkeypatch, capsys):
    seen = {}

    def fake_run(prompt, **kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(print_mode, "run", fake_run)
    monkeypatch.setattr(cli, "_settings", lambda: Settings(model="gpt-oss:20b"))
    assert cli.main(["--no-mcp", "hello"]) == 0
    assert seen["model"] == "gpt-oss:20b" and seen["explicit_model"] is True
    assert cli.main(["--no-mcp", "--model", "gemma3:12b", "hello"]) == 0
    assert seen["model"] == "gemma3:12b"
    monkeypatch.setattr(cli, "_settings", lambda: Settings(model="qwen2.5-coder:7b"))
    assert cli.main(["--no-mcp", "hello"]) == 2
    assert "banned" in capsys.readouterr().err


def test_cli_usage_names_bjorn(capsys):
    try:
        cli.main(["--help"])
    except SystemExit as e:
        assert e.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: bjorn ") and "security gate" in out


class _StubHooks:
    """Minimal HookRunner stand-in; records the events print_mode fires."""

    def __init__(self, stop_messages=()):
        self.events: list[str] = []
        self._stop = SimpleNamespace(stop_messages=list(stop_messages))

    def run(self, event, **_kw):
        self.events.append(event)
        return self._stop if event == "Stop" else SimpleNamespace(stop_messages=[])


def test_hooks_path_runs_turns_and_closes_the_session(monkeypatch, capsys, tmp_path):
    hooks = _StubHooks()
    monkeypatch.setattr(print_mode, "Session", _FakeSession)
    monkeypatch.setattr(print_mode, "build_hook_runner", lambda *a, **k: hooks)
    monkeypatch.setenv("HARNESS_TOOL_PROBE", "0")
    monkeypatch.chdir(tmp_path)

    rc = print_mode.run("hi", enable_mcp=False, output_format="json")

    assert rc == 3
    assert hooks.events == ["SessionStart", "UserPromptSubmit", "Stop"]
    body = json.loads(capsys.readouterr().out.strip())
    assert body["halted_reason"] == "deadline"


def test_stop_hook_feedback_drives_one_more_turn(monkeypatch, capsys, tmp_path):
    hooks = _StubHooks(stop_messages=["keep going"])
    seen: list[str] = []

    class _Recording(_FakeSession):
        def run_turn(self, prompt, *, deadline_s=None):
            seen.append(prompt)
            return _result(self.reason)

    monkeypatch.setattr(print_mode, "Session", _Recording)
    monkeypatch.setattr(print_mode, "build_hook_runner", lambda *a, **k: hooks)
    monkeypatch.setenv("HARNESS_TOOL_PROBE", "0")
    monkeypatch.chdir(tmp_path)

    print_mode.run("hi", enable_mcp=False, output_format="json")
    capsys.readouterr()

    assert seen == ["hi", "keep going"]
