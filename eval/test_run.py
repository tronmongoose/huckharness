"""Runner-level tests for eval/run.py and the committed pin. No model runs: subprocess is stubbed."""
from __future__ import annotations

import functools
import json
import os
import subprocess
import tempfile
from pathlib import Path

import gate
import run
from gate import load_baseline
from run import (
    HARNESS_MAX_TIME_S,
    RESULTS_DIR,
    TASKS_DIR,
    _run_harness,
    _run_once,
    _task_row,
)

SUMMARY = {"event": "summary", "turns": 4, "halted_reason": "model_done",
           "files_changed": ["/w/a.py"], "session_log": "/m/sessions/s.jsonl"}


class _Proc:
    def __init__(self, stderr: str):
        self.stderr = stderr
        self.returncode = 0


def _stub_run(monkeypatch, stderr: str, calls: list) -> None:
    def fake(cmd, **kw):
        calls.append((cmd, kw))
        return _Proc(stderr)
    monkeypatch.setattr(run.subprocess, "run", fake)


def test_run_harness_argv_env_and_summary(monkeypatch, tmp_path):
    calls: list = []
    _stub_run(monkeypatch, "noise\n" + json.dumps(SUMMARY) + "\n", calls)
    env = {"HARNESS_META_DIR": str(tmp_path)}
    out = _run_harness(tmp_path, "fix it", "m:tag", env)
    cmd, kw = calls[0]
    assert cmd[-1] == "fix it"
    assert cmd[cmd.index("--max-time") + 1] == str(HARNESS_MAX_TIME_S) == "540"
    assert cmd[cmd.index("--model") + 1] == "m:tag"
    assert "--force-local" in cmd and "--no-mcp" in cmd
    assert kw["env"] is env
    assert kw["cwd"] == str(tmp_path)
    assert out["steps"] == 4
    assert out["halted_reason"] == "model_done"
    assert out["files_changed"] == ["a.py"]
    assert isinstance(out["wall_s"], float)
    assert "--check" not in cmd


def test_run_harness_check_flags_from_task(monkeypatch, tmp_path):
    calls: list = []
    _stub_run(monkeypatch, json.dumps(SUMMARY) + "\n", calls)
    _run_harness(tmp_path, "fix it", None, {}, ["make test", "pytest -q tests"])
    cmd, _ = calls[0]
    flags = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--check"]
    assert flags == ["make test", "pytest -q tests"]
    assert cmd[-1] == "fix it"


def test_run_harness_tells_timeout_from_crash(monkeypatch, tmp_path):
    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)
    monkeypatch.setattr(run.subprocess, "run", boom)
    assert _run_harness(tmp_path, "p", None, {})["halted_reason"] == "timeout"
    _stub_run(monkeypatch, "Traceback (most recent call last):\nImportError: boom\n", [])
    out = _run_harness(tmp_path, "p", None, {})
    assert out["halted_reason"] is None
    assert out["steps"] is None
    assert out["files_changed"] == []


def _task(tmp_path: Path) -> dict:
    repo = tmp_path / "task" / "repo"
    repo.mkdir(parents=True)
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    return {"id": "t", "prompt": "p", "grade": {}, "checks": ["make test"],
            "_dir": tmp_path / "task"}


def _fake_harness(halted: str | None, seen: dict):
    def fake(wd, prompt, model, env, checks=None):
        seen["wd"], seen["meta"], seen["checks"] = wd, Path(env["HARNESS_META_DIR"]), checks
        seen["overlay"] = env.get("HARNESS_PROMPT_OVERLAY")
        seen["home"] = Path(env["HOME"])
        sessions = seen["meta"] / "sessions"
        sessions.mkdir(parents=True)
        (sessions / "s.jsonl").write_text(
            json.dumps({"kind": "error", "turn": 1, "error": "x"}) + "\n", encoding="utf-8")
        return {"steps": None, "halted_reason": halted, "files_changed": [], "wall_s": 1.5}
    return fake


def test_run_once_merges_metrics_and_cleans_up(monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(run, "_run_harness", _fake_harness(None, seen))
    monkeypatch.setattr(run, "grade", lambda spec, wd: (False, "exit 1"))
    opts = {"review": "off", "review_model": None, "keep": False, "overlay": None}
    row = _run_once(_task(tmp_path), None, opts)
    # No summary line: the transcript's error record is the halt reason.
    assert row["halted_reason"] == "error"
    assert row["passed"] is False and row["detail"] == "exit 1" and row["wall_s"] == 1.5
    assert row["tokens_in"] == 0 and row["tool_calls"] == 0
    assert seen["checks"] == ["make test"]
    assert not seen["wd"].exists() and not seen["meta"].exists()
    assert seen["home"].name.startswith("eval-home-t-") and not seen["home"].exists()

    monkeypatch.setattr(run, "_run_harness", _fake_harness("model_done", seen))
    assert _run_once(_task(tmp_path / "b"), None, opts)["halted_reason"] == "model_done"
    assert "meta_dir" not in row


def test_run_once_keep_records_meta_dir_and_threads_overlay(monkeypatch, tmp_path):
    seen: dict = {}
    monkeypatch.setattr(run.tempfile, "mkdtemp",
                        functools.partial(tempfile.mkdtemp, dir=str(tmp_path)))
    monkeypatch.setattr(run, "_run_harness", _fake_harness("model_done", seen))
    monkeypatch.setattr(run, "grade", lambda spec, wd: (True, ""))
    opts = {"review": "off", "review_model": None, "keep": True, "overlay": "/o/notes.md"}
    row = _run_once(_task(tmp_path), None, opts)
    assert row["meta_dir"] == str(seen["meta"]) and seen["meta"].is_dir()
    assert seen["wd"].is_dir() and seen["home"].is_dir()
    assert seen["home"].parent == seen["meta"].parent
    assert seen["overlay"] == "/o/notes.md"


def _r(passed: bool, steps: int, wall: float, **extra) -> dict:
    base = {"passed": passed, "steps": steps, "wall_s": wall, "detail": "",
            "edit_errors": 0, "bash_errors_harness": 0, "repair_rounds": 0}
    return {**base, **extra}


def test_task_row_aggregates_over_runs():
    runs = [_r(True, 3, 30.0, repair_rounds=1),
            _r(False, 9, 50.0, edit_errors=2, bash_errors_harness=1, detail="last"),
            _r(True, 5, 40.0, repair_rounds=2)]
    row = _task_row("t", runs)
    assert row["resolved"] is True and row["passes"] == 2 and row["runs"] == 3
    assert row["steps"] == 4  # median over passing runs only
    assert row["wall_s"] == 40.0
    assert (row["edit_errors"], row["bash_errors_harness"], row["repair_rounds"]) == (2, 1, 3)
    assert row["detail"] == ""
    assert row["per_run"] is runs

    row = _task_row("t", [runs[0], runs[1], runs[1]])
    assert row["resolved"] is False and row["steps"] == 3 and row["detail"] == "last"


def test_committed_baseline_is_schema_2_interim_with_fingerprint():
    b = load_baseline()
    assert b["schema"] == 2 and b["interim"] is True
    assert b["note"] == gate.INTERIM_NOTE
    fp = b["fingerprint"]
    for key in ("harness_sha", "ollama_version", "model", "model_digest"):
        assert fp[key] and fp[key] != "unknown"
    task_ids = sorted(json.loads(p.read_text(encoding="utf-8"))["id"]
                      for p in TASKS_DIR.glob("*/task.json"))
    assert sorted(b["per_task"]) == task_ids
    assert b["tasks"] == len(task_ids)
    for t in b["per_task"].values():
        assert t["runs"] == b["repeats"]
        assert t["must_pass"] == (t["passes"] == t["runs"])
    assert b["resolved"] == sum(1 for t in b["per_task"].values() if t["passes"] * 2 >= t["runs"])
    twins = list(RESULTS_DIR.glob(f"baseline-*-interim-{b['pinned']}.json"))
    assert len(twins) == 1
    assert json.loads(twins[0].read_text(encoding="utf-8"))["fingerprint"] == fp


HERMETIC = {"HARNESS_SETTINGS": "off", "HARNESS_RECALL": "0",
            "HARNESS_MEMORY_PROPOSALS": "0"}


def _operator_shell(monkeypatch, real_home: str) -> None:
    """A shell carrying the kinds of home paths an operator's env really has."""
    monkeypatch.setenv("PATH", f"{real_home}/.local/bin:/usr/bin")
    monkeypatch.setenv("VIRTUAL_ENV", f"{real_home}/proj/.venv")
    monkeypatch.setenv("PWD", f"{real_home}/proj")
    monkeypatch.setenv("XDG_CONFIG_HOME", f"{real_home}/.config")
    monkeypatch.setenv("HARNESS_MEMORY_DIR", f"{real_home}/.config/bjorn/memory")
    monkeypatch.setenv("HARNESS_SETTINGS", "on")
    monkeypatch.setenv("HARNESS_RECALL", "1")
    monkeypatch.setenv("ORCA_WORKSPACE_ID", f"ws:{real_home}/proj")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/agent.sock")
    monkeypatch.setenv("HARNESS_NUM_CTX", "4096")


def test_child_env_keeps_nothing_from_the_operators_home(monkeypatch, tmp_path):
    real_home = os.path.expanduser("~")
    _operator_shell(monkeypatch, real_home)
    meta, home = tmp_path / "meta", tmp_path / "home"
    env = run._child_env("off", None, str(meta), home_dir=str(home))
    leaked = {k: v for k, v in env.items()
              if v.startswith(real_home) and k not in ("PATH", "PYTHONPATH", "VIRTUAL_ENV")}
    assert leaked == {}
    assert env["HOME"] == str(home)
    assert env["HARNESS_MEMORY_DIR"] == str(meta / "memory")
    for key, value in HERMETIC.items():
        assert env[key] == value
    assert env["PATH"].startswith(f"{real_home}/.local/bin")
    assert real_home not in env["HOME"] and "HARNESS_CLAUDE_HOME" not in env
    for dropped in ("ORCA_WORKSPACE_ID", "SSH_AUTH_SOCK", "HARNESS_NUM_CTX", "PWD"):
        assert dropped not in env, dropped
    assert run._child_env("off", None, str(meta))["HOME"] == str(meta / "home")


def test_claude_cli_rows_get_the_temp_home_and_pass_the_real_one(monkeypatch, tmp_path):
    real_home = os.path.expanduser("~")
    _operator_shell(monkeypatch, real_home)
    meta, home = tmp_path / "meta", tmp_path / "home"
    env = run._child_env("claude-cli", "opus", str(meta), home_dir=str(home))
    assert env["HOME"] == str(home)
    assert env["HARNESS_CLAUDE_HOME"] == real_home
    assert env["HARNESS_REVIEW_BACKEND"] == "claude-cli"
    assert env["HARNESS_MEMORY_DIR"] == str(meta / "memory")
    for key, value in HERMETIC.items():
        assert env[key] == value


def test_eval_child_memory_grant_and_prompt_stay_in_the_run(monkeypatch, tmp_path):
    from coding_harness.context.memory import memory_dir
    from coding_harness.core.envelope import preset
    from coding_harness.core.mode import Autonomy
    from coding_harness.modes.print_mode import build_system_prompt

    real_home = os.path.expanduser("~")
    real_mem = os.path.join(real_home, ".config", "bjorn", "memory")
    meta, home = tmp_path / "eval-meta-t-x", tmp_path / "eval-home-t-x"
    env = run._child_env("off", None, str(meta), home_dir=str(home))
    wd = Path(tempfile.mkdtemp(prefix="eval-t-", dir=str(tmp_path)))
    for key in list(os.environ):
        monkeypatch.delenv(key)
    for key, value in {**env, "HARNESS_TOOL_PROBE": "0"}.items():
        monkeypatch.setenv(key, value)
    mem = memory_dir(str(wd))
    assert mem.name == wd.name and mem.is_relative_to(meta)
    envelope = preset(str(wd), Autonomy.LOW)
    assert envelope.check("Write", {"file_path": str(mem / "MEMORY.md")}).allowed
    leak = os.path.join(real_mem, wd.name, "MEMORY.md")
    assert not envelope.check("Write", {"file_path": leak}).allowed
    prompt = build_system_prompt(str(wd), include_repo_map=False)
    assert f"lives in {mem}." in prompt and real_mem not in prompt
