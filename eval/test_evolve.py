"""Prompt evolution loop: scoring, acceptance and lineage with the runner and model faked."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import evolve  # noqa: E402


def _row(task, passes, resolved, steps=3, detail="exit 1"):
    return {"id": task, "resolved": resolved, "passes": passes, "steps": steps,
            "detail": detail, "per_run": [{"halted_reason": "model_done", "steps": steps,
                                          "edit_errors": 0, "bash_errors_harness": 0}]}


def _runner(script):
    """Fake run_eval: the overlay file passed in picks which canned rows come back."""
    calls = []

    def run(model, tasks, repeats, keep_workdirs=False, overlay=None):
        text = Path(overlay).read_text().strip() if overlay else ""
        calls.append(text)
        return script.get(text, script[""])
    run.calls = calls
    return run


def test_score_passes_the_overlay_file_to_the_runner_not_the_env(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_PROMPT_OVERLAY", raising=False)
    run = _runner({"": [_row("a", 1, True)], "notes": [_row("a", 0, False)]})
    opts = {"workdir": str(tmp_path), "tasks": [], "repeats": 1, "keep": False}
    c = evolve.score("notes", None, opts, run)
    assert c.resolved == 0 and run.calls == ["notes"]
    assert "HARNESS_PROMPT_OVERLAY" not in os.environ
    assert evolve.score("", None, opts, run).resolved == 1
    assert run.calls == ["notes", ""]


def test_clip_falls_back_to_a_byte_cut_without_newlines():
    text = "x" * (evolve.OVERLAY_CAP_BYTES + 100)
    clipped = evolve._clip(text)
    assert clipped and len(clipped.encode()) == evolve.OVERLAY_CAP_BYTES
    lines = "\n".join("- rule" for _ in range(2000))
    assert evolve._clip(lines).endswith("- rule")


def test_transcript_tail_reads_errors_and_final_text(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    recs = [{"kind": "tool_result", "name": "Edit", "content": "old_string not found", "is_error": True},
            {"kind": "tool_result", "name": "Bash", "content": "ok", "is_error": False},
            {"kind": "assistant_message", "content": "", "tool_calls": [{"id": "1"}]},
            {"kind": "assistant_message", "content": "Done, tests pass."}]
    (sessions / "s.jsonl").write_text("\n".join(json.dumps(r) for r in recs) + "\nnot json\n")
    tail = evolve._transcript_tail(str(tmp_path))
    assert tail == "old_string not found | final: Done, tests pass."
    assert evolve._transcript_tail(None) == "" and evolve._transcript_tail(str(tmp_path / "x")) == ""
    rows = [_row("bad", 0, False)]
    rows[0]["per_run"][-1]["meta_dir"] = str(tmp_path)
    assert "trace: old_string not found" in evolve.failure_traces(rows)[0]


def test_failure_traces_only_failing_tasks_and_capped():
    rows = [_row("ok", 1, True)] + [_row(f"bad{i}", 0, False) for i in range(10)]
    traces = evolve.failure_traces(rows)
    assert len(traces) == evolve.MAX_TRACES
    assert all("task: bad" in t and "grader: exit 1" in t for t in traces)


def test_propose_clips_to_cap_and_refuses_banned_models(monkeypatch):
    long_reply = {"content": "\n".join("- rule" for _ in range(2000))}
    text = evolve.propose("", ["t"], "mistral-small3.2", chat=lambda **kw: long_reply)
    assert len(text.encode()) <= evolve.OVERLAY_CAP_BYTES and text.endswith("- rule")
    with pytest.raises(ValueError):
        evolve.propose("", ["t"], "qwen2.5-coder", chat=lambda **kw: long_reply)


def test_evolve_keeps_a_child_that_resolves_a_new_task(tmp_path):
    script = {
        "": [_row("a", 1, True), _row("b", 0, False)],
        "- be careful": [_row("a", 1, True), _row("b", 1, True)],
    }
    run = _runner(script)
    opts = {"workdir": str(tmp_path), "tasks": [], "repeats": 1, "keep": False,
            "reflect_model": "mistral-small3.2"}
    chat = lambda **kw: {"content": "- be careful"}  # noqa: E731
    cands, best = evolve.evolve("", None, opts, rounds=3, budget_s=60, chat=chat, run=run)
    assert cands[best].text == "- be careful" and cands[best].resolved == 2
    assert cands[1].parent == 0 and cands[1].accepted
    # the improved parent resolves everything, so the loop stops early
    assert len(cands) == 2


def test_evolve_drops_a_child_that_loses_a_task(tmp_path):
    script = {
        "": [_row("a", 1, True), _row("b", 0, False)],
        "- worse": [_row("a", 0, False), _row("b", 1, True), _row("c", 0, False)],
    }
    run = _runner({**script, "- worse": [_row("a", 0, False), _row("b", 0, False)]})
    opts = {"workdir": str(tmp_path), "tasks": [], "repeats": 1, "keep": False,
            "reflect_model": "mistral-small3.2"}
    cands, best = evolve.evolve("", None, opts, rounds=1, budget_s=60,
                                chat=lambda **kw: {"content": "- worse"}, run=run)
    assert best == 0 and not cands[1].accepted


def test_write_results_writes_lineage_and_best(tmp_path):
    a = evolve.Candidate(text="", resolved=1, passes={"a": 1}, accepted=True)
    b = evolve.Candidate(text="- x", parent=0, resolved=2, passes={"a": 1, "b": 1}, accepted=True)
    path = evolve.write_results([a, b], 1, tmp_path)
    assert re.fullmatch(r"evolve-\d{4}-\d{2}-\d{2}-\d{4}\.json", path.name)
    data = json.loads(path.read_text())
    assert data["best"] == 1 and data["candidates"][1]["parent"] == 0
    assert (tmp_path / "overlay-best.md").read_text() == "- x\n"
