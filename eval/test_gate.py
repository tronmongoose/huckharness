"""Gate decision, pinned child env, metrics, fingerprint and compare tests. No model runs."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import gate
import pytest
from compare import reproducible
from fingerprint import default_model
from gate import fingerprint_mismatch, load_baseline, verdict
from metrics import session_metrics
from run import _child_env, _report

BASE = {"resolved": 10, "tasks": 11, "slack": 1, "per_task": {}, "fingerprint": None}
EVAL_DIR = Path(__file__).resolve().parent


def _rows(resolved: int, total: int = 11) -> list[dict]:
    return [{"id": f"t{i}", "resolved": i < resolved} for i in range(total)]


def test_pass_at_baseline():
    assert verdict(_rows(10), BASE)["passed"]


def test_pass_within_slack():
    assert verdict(_rows(9), BASE)["passed"]


def test_fail_below_floor():
    v = verdict(_rows(8), BASE)
    assert not v["passed"]
    assert len(v["regressed"]) == 3


def test_subset_run_fails():
    # A partial run must never pass the gate, even if everything resolves.
    assert not verdict(_rows(5, total=5), BASE)["passed"]


def test_must_pass_regression_fails_inside_slack():
    base = dict(BASE, per_task={
        "t10": {"passes": 3, "runs": 3, "must_pass": True},
        "t0": {"passes": 2, "runs": 3, "must_pass": False},
    })
    # t10 unresolved: aggregate 10/11 clears the floor of 9, the per-task pin does not.
    v = verdict(_rows(10), base)
    assert not v["passed"]
    assert v["must_pass_failed"] == ["t10"]
    # A task that was already flaky at pin time may still drop inside slack.
    rows = _rows(11)
    rows[0]["resolved"] = False
    v = verdict(rows, base)
    assert v["passed"]
    assert v["must_pass_failed"] == []


def test_missing_baseline_is_hard_error(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "BASELINE_PATH", tmp_path / "absent.json")
    with pytest.raises(SystemExit):
        load_baseline()


def test_malformed_baseline_is_hard_error(tmp_path, monkeypatch):
    bad = tmp_path / "baseline.json"
    bad.write_text(json.dumps({"resolved": "ten", "tasks": 11, "slack": 1}))
    monkeypatch.setattr(gate, "BASELINE_PATH", bad)
    with pytest.raises(SystemExit):
        load_baseline()


def test_schema_1_baseline_still_loads(tmp_path, monkeypatch):
    old = tmp_path / "baseline.json"
    old.write_text(json.dumps({"model": "default-local", "tasks": 11, "resolved": 10,
                               "repeats": 3, "slack": 1, "pinned": "2026-08-10"}))
    monkeypatch.setattr(gate, "BASELINE_PATH", old)
    b = load_baseline()
    assert b["schema"] == 1
    assert b["per_task"] == {}
    assert b["fingerprint"] is None
    assert fingerprint_mismatch(b["fingerprint"], {"model_digest": "x", "ollama_version": "y"}) == []
    assert verdict(_rows(10), b)["passed"]


def test_pin_writes_schema_2_interim(tmp_path, monkeypatch):
    monkeypatch.setattr(gate, "BASELINE_PATH", tmp_path / "baseline.json")
    rows = [{"id": "a", "resolved": True, "passes": 3, "runs": 3},
            {"id": "b", "resolved": True, "passes": 2, "runs": 3}]
    b = gate.pin(rows, 3, "default-local", {"model_digest": "d1", "ollama_version": "0.20.0"})
    assert b["schema"] == 2
    assert b["interim"] is True
    assert b["note"] == gate.INTERIM_NOTE
    assert b["per_task"]["a"]["must_pass"] is True
    assert b["per_task"]["b"]["must_pass"] is False
    assert load_baseline()["fingerprint"]["model_digest"] == "d1"


def test_fingerprint_mismatch_compares_model_server_and_profile_only():
    pinned = {"harness_sha": "aaa", "harness_dirty": False,
              "model_digest": "d1", "ollama_version": "0.20.0", "profile_hash": "p1"}
    assert fingerprint_mismatch(pinned, dict(pinned, harness_sha="bbb", harness_dirty=True)) == []
    assert len(fingerprint_mismatch(pinned, dict(pinned, model_digest="d2"))) == 1
    both = dict(pinned, model_digest="d2", ollama_version="0.21.0")
    assert len(fingerprint_mismatch(pinned, both)) == 2
    sampling = fingerprint_mismatch(pinned, dict(pinned, profile_hash="p2"))
    assert len(sampling) == 1 and sampling[0].startswith("profile_hash")


def test_fingerprint_carries_profile_hash(monkeypatch):
    import fingerprint as fp_mod

    from coding_harness.models.profile import profile_hash, resolve_profile

    monkeypatch.setattr(fp_mod, "ollama_version", lambda url: "0.20.0")
    monkeypatch.setattr(fp_mod, "model_digest", lambda url, model: "d1")
    for key in ("HARNESS_TEMPERATURE", "HARNESS_NUM_CTX"):
        monkeypatch.delenv(key, raising=False)
    fp = fp_mod.fingerprint("mistral-small3.2:latest")
    assert fp["profile_hash"] == profile_hash(resolve_profile("mistral-small3.2:latest"))
    assert fp["profile_hash"] == fp_mod.model_profile_hash("mistral-small3.2:latest")
    monkeypatch.setenv("HARNESS_TEMPERATURE", "0.9")
    assert fp_mod.fingerprint("mistral-small3.2:latest")["profile_hash"] != fp["profile_hash"]


def test_pin_without_profile_hash_mismatches_until_repinned():
    # The phase 0 pin predates the profile layer; it must read as a different
    # baseline (exit 3), not crash, until P2-1 re-pins with the hash.
    pinned = {"harness_sha": "aaa", "model_digest": "d1", "ollama_version": "0.20.0"}
    live = dict(pinned, profile_hash="p1")
    assert fingerprint_mismatch(pinned, live) == ["profile_hash: pinned None, live 'p1'"]


def _stub_gate(monkeypatch, pinned_fp: dict, live_fp: dict, rows: list[dict]) -> list:
    calls: list = []
    monkeypatch.setattr(gate, "load_baseline", lambda: dict(BASE, fingerprint=pinned_fp))
    monkeypatch.setattr(gate, "run_meta", lambda *a: {"fingerprint": live_fp})
    monkeypatch.setattr(gate, "run_eval", lambda *a, **k: calls.append(a) or rows)
    return calls


def test_fingerprint_mismatch_refuses_before_running(monkeypatch, capsys):
    pinned = {"model_digest": "d1", "ollama_version": "0.20.0"}
    calls = _stub_gate(monkeypatch, pinned, dict(pinned, model_digest="d2"), _rows(10))
    assert gate.main([]) == gate.EXIT_FINGERPRINT
    assert calls == []
    assert "model_digest" in capsys.readouterr().out


def test_fingerprint_mismatch_override_runs_the_gate(monkeypatch):
    pinned = {"model_digest": "d1", "ollama_version": "0.20.0"}
    calls = _stub_gate(monkeypatch, pinned, dict(pinned, model_digest="d2"), _rows(10))
    assert gate.main(["--allow-fingerprint-mismatch"]) == 0
    assert len(calls) == 1


def test_matching_fingerprint_runs_and_fails_on_regression(monkeypatch):
    fp = {"model_digest": "d1", "ollama_version": "0.20.0"}
    calls = _stub_gate(monkeypatch, fp, dict(fp, harness_sha="new"), _rows(8))
    assert gate.main([]) == 1
    assert len(calls) == 1


def test_child_env_pins_switches_and_drops_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("HARNESS_REVIEW", "1")
    monkeypatch.setenv("HARNESS_REVIEW_BACKEND", "claude-cli")
    monkeypatch.setenv("HARNESS_REVIEW_MODEL", "sonnet")
    monkeypatch.setenv("HARNESS_EDIT_HINT", "0")
    monkeypatch.setenv("OLLAMA_URL", "http://ollama.local:11434")
    monkeypatch.setenv("HARNESS_PROMPT_OVERLAY", "/stale/notes.md")
    env = _child_env("off", None, "/tmp/meta-x")
    assert "ANTHROPIC_API_KEY" not in env
    assert "HARNESS_PROMPT_OVERLAY" not in env
    assert env["HARNESS_REVIEW"] == "0"
    assert "HARNESS_REVIEW_BACKEND" not in env
    assert "HARNESS_REVIEW_MODEL" not in env
    assert env["HARNESS_META_DIR"] == "/tmp/meta-x"
    for key in ("HARNESS_VERIFY_REPAIR", "HARNESS_REPO_MAP", "HARNESS_EDIT_HINT", "HARNESS_TOOL_PROBE"):
        assert env[key] == "1"
    assert env["OLLAMA_URL"] == "http://ollama.local:11434"
    assert env["PYTHONPATH"].split(":")[0] == str(EVAL_DIR.parent)

    env = _child_env("claude-cli", "opus", "/tmp/meta-y", overlay="/o/notes.md")
    assert env["HARNESS_PROMPT_OVERLAY"] == "/o/notes.md"
    assert env["HARNESS_REVIEW"] == "1"
    assert env["HARNESS_REVIEW_BACKEND"] == "claude-cli"
    assert env["HARNESS_REVIEW_MODEL"] == "opus"
    assert "ANTHROPIC_API_KEY" not in env


def _rec(kind: str, **extra) -> str:
    return json.dumps({"ts": "2026-08-29T00:00:00Z", "kind": kind, **extra})


def test_session_metrics_on_canned_transcript_and_audit(tmp_path):
    log = tmp_path / "sessions" / "s.jsonl"
    log.parent.mkdir()
    log.write_text("\n".join([
        _rec("session_start", session_id="s", model="m"),
        _rec("user_message", content="fix it", turn=1),
        _rec("assistant_message", turn=1, content="", tool_calls=[{"id": "c1"}]),
        _rec("tool_result", turn=1, name="Read", is_error=False, error_class=None),
        _rec("tool_result", turn=2, name="Edit", is_error=True, error_class=None),
        _rec("tool_result", turn=3, name="Edit", is_error=False, error_class=None),
        _rec("tool_result", turn=3, name="Write", is_error=False, error_class=None),
        _rec("verify_repair", turn=3, round=1, files=["a.py"]),
        _rec("verify_repair", round=1, files=["a.py"]),
        _rec("tool_result", turn=4, name="Bash", is_error=True,
             error_class="harness_caused:command_not_found"),
        _rec("tool_result", turn=5, name="Bash", is_error=True, error_class="informative"),
        _rec("tool_result", turn=6, name="Bash", is_error=False, error_class=None),
        _rec("agentic_review", round=1, approved=False, files=["a.py"]),
        _rec("agentic_review", round=1, approved=False),
        _rec("agentic_review", round=2, approved=True),
        _rec("review_skipped", reason="deadline_reserve", remaining_s=10.0),
        "not json",
        _rec("turn_done", turn=1, halted_reason="model_done", turns=7),
    ]) + "\n", encoding="utf-8")
    (tmp_path / "audit.jsonl").write_text("\n".join([
        json.dumps({"kind": "turn", "tokens_in": 1000, "tokens_out": 50, "inner_steps": 6}),
        json.dumps({"tool": "Read", "args_digest": "x", "allowed": True}),
        json.dumps({"kind": "turn", "tokens_in": 200, "tokens_out": 5, "inner_steps": 1}),
    ]) + "\n", encoding="utf-8")
    assert session_metrics(log, tmp_path) == {
        "tool_calls": 7, "edit_calls": 3, "edit_errors": 1,
        "bash_calls": 3, "bash_errors_informative": 1, "bash_errors_harness": 1,
        "repair_rounds": 1, "review_rounds": 2, "halted_reason": "model_done",
        "tokens_in": 1200, "tokens_out": 55, "inner_steps": 7,
    }


def test_session_metrics_missing_files_yield_zeros(tmp_path):
    m = session_metrics(tmp_path / "absent.jsonl", tmp_path / "no-meta")
    assert m["tool_calls"] == 0
    assert m["tokens_in"] == 0
    assert m["halted_reason"] is None


def test_session_metrics_deadline_record_implies_halt_reason(tmp_path):
    log = tmp_path / "s.jsonl"
    log.write_text(_rec("deadline", turn=1, at_step=4, files_changed=[]) + "\n", encoding="utf-8")
    assert session_metrics(log, tmp_path)["halted_reason"] == "deadline"


def _task(tid: str, passes: int) -> dict:
    return {"id": tid, "passes": passes, "runs": 3, "resolved": passes * 2 >= 3}


def test_compare_verdict_on_two_reports(tmp_path):
    a = [_task("x", 3), _task("y", 2), _task("z", 1)]
    b = [_task("x", 3), _task("y", 1), _task("z", 2)]
    r = reproducible(a, b, slack=1)
    assert r["reproducible"] is True
    assert r["drift"] == {}
    assert r["resolved"] == [2, 2]

    c = [_task("x", 3), _task("y", 0), _task("z", 1)]
    r = reproducible(a, c, slack=1)
    assert r["reproducible"] is False
    assert r["drift"] == {"y": [2, 0]}

    import compare
    pa, pb = tmp_path / "a.json", tmp_path / "c.json"
    pa.write_text(json.dumps({"rows": a, "fingerprint": {"harness_sha": "s1"}}))
    pb.write_text(json.dumps({"rows": c, "fingerprint": {"harness_sha": "s2"}}))
    assert compare.main([str(pa), str(pb)]) == 1
    pb.write_text(json.dumps({"rows": b, "fingerprint": {}}))
    assert compare.main([str(pa), str(pb)]) == 0


def test_report_has_pass_fraction_and_fingerprint_footer():
    rows = [{"id": "x", "resolved": True, "passes": 2, "runs": 3, "steps": 3.0, "detail": "",
             "wall_s": 40.5, "edit_errors": 1, "bash_errors_harness": 0, "repair_rounds": 2},
            {"id": "y", "resolved": False, "passes": 0, "runs": 3, "steps": None,
             "detail": "exit 1 (want 0): Traceback\n  File x\nAssertionError",
             "wall_s": 12.0, "edit_errors": 0, "bash_errors_harness": 1, "repair_rounds": 0}]
    text = _report(rows, "default-local", 3, {"harness_sha": "abc", "model_digest": "d1"})
    assert "| x | 2/3 | 0.67 | 3.0 | 40.5 | 1 | 0 | 2 |  |" in text
    # A multi-line grader detail must stay on one table row.
    assert "| y | 0/3 | 0.00 | None | 12.0 | 0 | 1 | 0 | exit 1 (want 0): Traceback File x AssertionError |" in text
    assert "- harness_sha: abc" in text
    assert "- model_digest: d1" in text


def test_run_module_import_keeps_the_subprocess_boundary():
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import run, gate, compare; "
            "print(sorted(m for m in sys.modules if m.startswith('coding_harness')))")
    proc = subprocess.run([sys.executable, "-c", code, str(EVAL_DIR)],
                          capture_output=True, text=True, check=True)
    assert proc.stdout.strip() == "[]"


def test_default_model_is_the_harness_default():
    from coding_harness.modes.print_mode import DEFAULT_MODEL
    assert default_model() == DEFAULT_MODEL
