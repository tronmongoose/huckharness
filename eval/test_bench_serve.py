"""Pure-function tests for the serving bench and step-time analysis. No network."""
import threading
from datetime import date, datetime, timezone

import bench_serve
from bench_serve import (
    FILLER_LINES,
    derive,
    filler_prompt,
    queue_wait,
    server_gap,
    tok_per_s,
)
from step_times import (
    analyze,
    load_sessions,
    overlap_count,
    percentiles,
    step_intervals,
)

NS = 1_000_000_000


def test_tok_per_s_derivation():
    assert tok_per_s(256, 8 * NS) == 32.0
    assert tok_per_s(0, 8 * NS) is None
    assert tok_per_s(256, None) is None


def test_queue_wait_is_wall_minus_server_time():
    assert queue_wait(12.5, 10 * NS) == 2.5
    assert queue_wait(12.5, None) is None


def test_derive_rates_and_queue_wait():
    resp = {"load_duration": 2 * NS, "prompt_eval_count": 6000,
            "prompt_eval_duration": 12 * NS, "eval_count": 256,
            "eval_duration": 16 * NS, "total_duration": 30 * NS, "done_reason": "length"}
    d = derive(resp, 31.0)
    assert d["prefill_tps"] == 500.0
    assert d["decode_tps"] == 16.0
    assert d["load_s"] == 2.0
    assert d["prefill_s"] == 12.0
    assert d["queue_wait_s"] == 1.0
    assert d["server_gap_s"] == 0.0
    assert d["done_reason"] == "length"


def test_server_gap_exposes_in_server_queueing():
    # A cache hit that still took 100s server-side: the wait is inside total_duration.
    resp = {"load_duration": NS, "prompt_eval_duration": NS, "eval_duration": NS,
            "total_duration": 103 * NS}
    assert server_gap(resp) == 100.0
    assert server_gap({}) is None


def test_filler_prompt_is_about_six_thousand_tokens():
    p = filler_prompt()
    assert p.count("\n") == FILLER_LINES + 1
    # Measured on mistral-small3.2: 500 lines tokenized to 11,020 tokens, so
    # 270 lines lands near 6k. Pin the line shape so that calibration holds.
    assert len(p.splitlines()[-1]) == 68
    assert filler_prompt() == p


def _rec(ts: str, kind: str, **extra) -> dict:
    return {"ts": ts, "kind": kind, **extra}


CANNED = [
    _rec("2026-08-25T10:00:00Z", "session_start"),
    _rec("2026-08-25T10:00:00Z", "user_message"),
    _rec("2026-08-25T10:00:30Z", "assistant_message", turn=1),
    _rec("2026-08-25T10:00:31Z", "tool_result", turn=1),
    _rec("2026-08-25T10:02:01Z", "assistant_message", turn=2),
    _rec("2026-08-25T10:02:02Z", "tool_result", turn=2),
    _rec("2026-08-25T10:05:02Z", "error", turn=3, error="timeout: timed out"),
    _rec("2026-08-25T10:05:02Z", "turn_done", turn=1),
]


def test_step_intervals_include_timed_out_step():
    steps = step_intervals(CANNED)
    assert [round(s["seconds"]) for s in steps] == [30, 90, 180]
    assert [s["timed_out"] for s in steps] == [False, False, True]


def test_percentiles_nearest_rank():
    p = percentiles([float(i) for i in range(1, 101)])
    assert (p["p50"], p["p90"], p["p99"], p["max"], p["n"]) == (50.0, 90.0, 99.0, 100.0, 100)
    assert percentiles([])["p50"] is None


def test_overlap_counts_other_sessions_in_flight():
    other = step_intervals([
        _rec("2026-08-25T10:04:00Z", "user_message"),
        _rec("2026-08-25T10:06:00Z", "assistant_message", turn=1),
    ])
    idle = step_intervals([
        _rec("2026-08-25T10:00:00Z", "user_message"),
        _rec("2026-08-25T10:00:05Z", "assistant_message", turn=1),
    ])
    instant = datetime(2026, 8, 25, 10, 5, 2, tzinfo=timezone.utc)
    assert overlap_count(instant, {"other": other, "idle": idle}) == ["other"]


def test_analyze_end_to_end_on_canned_sessions():
    other = [
        _rec("2026-08-25T10:04:00Z", "user_message"),
        _rec("2026-08-25T10:06:00Z", "assistant_message", turn=1),
    ]
    r = analyze({"a": CANNED, "b": other})
    assert r["steps_completed"] == 3
    assert r["over_120s"] == 0
    assert len(r["timeouts"]) == 1
    assert r["timeouts"][0]["overlap"] == 1
    assert r["timeouts"][0]["overlapping_sessions"] == ["b"]
    assert r["timeouts_with_overlap"] == 1


def test_cache_probe_flags_reuse_by_prefill_duration_ratio(monkeypatch):
    # Ollama 0.20.0 reports the full prompt_eval_count on a hit, so the count
    # delta is 0 and only the duration ratio can carry the reuse signal.
    hit = {"prompt_eval_count": 6000, "prefill_s": 0.07}
    monkeypatch.setattr(bench_serve, "_chat", lambda *a: dict(hit))
    cold = {"prompt_eval_count": 6000, "prefill_s": 54.3}
    r = bench_serve.bench_cache("http://x", "m", "p", cold)
    assert r["prompt_eval_count_delta"] == 0
    assert r["prefill_duration_ratio"] < 0.1
    assert r["prefix_reused"] is True
    monkeypatch.setattr(bench_serve, "_chat", lambda *a: dict(hit, prefill_s=54.0))
    assert bench_serve.bench_cache("http://x", "m", "p", cold)["prefix_reused"] is False


def test_queue_probe_reports_max_wait_and_survives_one_failure(monkeypatch):
    outcomes = iter([{"queue_wait_s": 0.1, "server_gap_s": 54.2}, RuntimeError("transport: boom")])
    lock = threading.Lock()

    def fake_chat(*a):
        with lock:
            r = next(outcomes)
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(bench_serve, "_chat", fake_chat)
    r = bench_serve.bench_queue("http://x", "m", "p")
    assert r["max_server_gap_s"] == 54.2
    assert r["max_queue_wait_s"] == 0.1
    assert sum("error" in r[t] for t in ("a", "b")) == 1
    assert r["both_wall_s"] >= 0


def test_table_renders_error_records_without_raising():
    m = {"cold_load": {"error": "RuntimeError: transport: refused"},
         "prefill_decode": {"prefill_tps": 114.0, "wall_s": 54.3},
         "queue_probe": {"a": {"error": "x"}, "b": {"queue_wait_s": 0.0, "server_gap_s": 54.2}}}
    text = bench_serve._table(m)
    assert "cold load s" in text
    assert "err" in text
    assert "114.0" in text
    assert "err / 54.2" in text


def test_load_sessions_filters_by_since_and_skips_bad_lines(tmp_path):
    (tmp_path / "old.jsonl").write_text(
        '{"ts": "2026-08-01T00:00:00Z", "kind": "user_message"}\n', encoding="utf-8")
    (tmp_path / "new.jsonl").write_text(
        'not json\n'
        '{"ts": "2026-08-25T00:00:00Z", "kind": "user_message"}\n'
        '{"ts": "2026-08-25T00:03:10Z", "kind": "assistant_message", "turn": 1}\n',
        encoding="utf-8")
    sessions = load_sessions(tmp_path, date(2026, 8, 20))
    assert list(sessions) == ["new"]
    assert [r["kind"] for r in sessions["new"]] == ["user_message", "assistant_message"]
    r = analyze(sessions)
    assert r["steps_completed"] == 1
    assert r["over_120s"] == 1
    assert r["over_180s"] == 1
