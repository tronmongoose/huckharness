"""Transcript replay tests.

Covers the record-to-message mapping, call-id pairing, compaction
reproduction, and the fail-closed guard on pre-sl-a8kf transcripts.
"""
from __future__ import annotations

import json
from pathlib import Path

from coding_harness.core import stuck
from coding_harness.core.compaction import SUMMARY_MARKER
from coding_harness.core.replay import read_records, rebuild_messages

SYS = "system prompt v2"


def _write(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


def _assert_well_formed(messages: list[dict]) -> None:
    """Every tool result follows an assistant tool_call it can answer; Ollama rejects orphans."""
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        j = i
        while messages[j - 1].get("role") == "tool":
            j -= 1
        owner = messages[j - 1]
        assert owner.get("role") == "assistant", f"orphan tool result at {i}"
        assert i - j + 1 <= len(owner.get("tool_calls") or []), f"tool result at {i} has no call"


def _call_records(cid: str) -> list[dict]:
    """An assistant tool call and its logged result."""
    return [
        {"kind": "assistant_message", "content": "",
         "tool_calls": [{"id": cid, "function": {"name": "Read", "arguments": "{}"}}]},
        {"kind": "tool_result", "tool_call_id": cid, "name": "Read", "content": "ok"},
    ]


def test_plain_turn_roundtrip(tmp_path):
    path = _write(tmp_path, [
        {"kind": "session_start", "session_id": "s1", "model": "m", "mode": "act",
         "identity": "nightshift-agent", "envelope": None},
        {"kind": "user_message", "content": "hello"},
        {"kind": "assistant_message", "content": "hi", "tool_calls": []},
    ])
    r = rebuild_messages(path, SYS)
    assert r.complete
    assert [m["role"] for m in r.messages] == ["system", "user", "assistant"]
    assert r.messages[0]["content"] == SYS
    assert r.session_id == "s1"
    assert r.agent_identity == "nightshift-agent"
    assert r.user_turns == 1


def test_tool_call_and_result_are_paired(tmp_path):
    path = _write(tmp_path, [
        {"kind": "session_start", "session_id": "s2", "model": "m", "mode": "act"},
        {"kind": "user_message", "content": "run it"},
        {"kind": "assistant_message", "content": "",
         "tool_calls": [{"function": {"name": "Bash", "arguments": "{}"}}]},
        {"kind": "tool_result", "tool_call_id": "call_1_abc", "name": "Bash",
         "content": "ok", "is_error": False},
        {"kind": "assistant_message", "content": "done", "tool_calls": []},
    ])
    r = rebuild_messages(path, SYS)
    assert r.complete
    assert [m["role"] for m in r.messages] == [
        "system", "user", "assistant", "tool", "assistant"]
    # The id generated at dispatch time is backfilled onto the tool_call so the
    # replayed pair is internally consistent.
    assert r.messages[2]["tool_calls"][0]["id"] == "call_1_abc"
    assert r.messages[3]["tool_call_id"] == "call_1_abc"
    _assert_well_formed(r.messages)


def test_model_supplied_call_id_is_not_overwritten(tmp_path):
    path = _write(tmp_path, [
        {"kind": "assistant_message", "content": "",
         "tool_calls": [{"id": "orig", "function": {"name": "Bash", "arguments": "{}"}}]},
        {"kind": "tool_result", "tool_call_id": "orig", "name": "Bash", "content": "x"},
    ])
    r = rebuild_messages(path, SYS)
    assert r.messages[1]["tool_calls"][0]["id"] == "orig"


def test_legacy_transcript_without_results_is_incomplete(tmp_path):
    path = _write(tmp_path, [
        {"kind": "user_message", "content": "go"},
        {"kind": "assistant_message", "content": "",
         "tool_calls": [{"function": {"name": "Bash", "arguments": "{}"}}]},
        {"kind": "assistant_message", "content": "done", "tool_calls": []},
    ])
    r = rebuild_messages(path, SYS)
    assert not r.complete
    assert any("cannot be resumed faithfully" in w for w in r.warnings)


def test_compaction_collapses_history(tmp_path):
    records = [{"kind": "user_message", "content": f"u{i}"} for i in range(4)]
    # 1 system + 4 user = 5 replayed messages; compaction keeps system +
    # summary + 1 tail message.
    records.append({"kind": "compaction", "messages_before": 5, "messages_after": 3,
                    "summary": "earlier work"})
    records.append({"kind": "assistant_message", "content": "after", "tool_calls": []})
    r = rebuild_messages(_write(tmp_path, records), SYS)
    assert [m["role"] for m in r.messages] == ["system", "assistant", "user", "assistant"]
    assert r.messages[1]["content"].startswith(SUMMARY_MARKER)
    assert r.messages[2]["content"] == "u3"  # the verbatim tail


def test_unusable_compaction_record_warns_and_preserves(tmp_path):
    path = _write(tmp_path, [
        {"kind": "user_message", "content": "u"},
        {"kind": "compaction", "summary": "s"},  # no messages_after
    ])
    r = rebuild_messages(path, SYS)
    assert any("uncompacted" in w for w in r.warnings)
    assert [m["role"] for m in r.messages] == ["system", "user"]


def test_truncated_final_line_is_skipped(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text(
        json.dumps({"kind": "user_message", "content": "ok"}) + "\n"
        + '{"kind": "assistant_mess',  # killed mid-write
        encoding="utf-8",
    )
    assert len(read_records(p)) == 1
    r = rebuild_messages(p, SYS)
    assert [m["role"] for m in r.messages] == ["system", "user"]


def test_missing_transcript_is_empty_not_fatal(tmp_path):
    r = rebuild_messages(tmp_path / "absent.jsonl", SYS)
    assert [m["role"] for m in r.messages] == ["system"]
    assert r.user_turns == 0


def test_stuck_nudge_rides_the_preceding_tool_result(tmp_path):
    path = _write(tmp_path, [
        {"kind": "session_start", "session_id": "s7", "model": "m", "mode": "act"},
        {"kind": "user_message", "content": "find it"},
        {"kind": "assistant_message", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "Grep", "arguments": "{}"}}]},
        {"kind": "tool_result", "tool_call_id": "c1", "name": "Grep",
         "content": "no matches", "is_error": False},
        {"kind": "stuck_nudge", "turn": 2, "run": 2, "calls": [["Grep", "k"]],
         "text": stuck.nudge_text},
        {"kind": "assistant_message", "content": "done", "tool_calls": []},
    ])
    r = rebuild_messages(path, SYS)
    assert r.complete
    assert [m["role"] for m in r.messages] == [
        "system", "user", "assistant", "tool", "assistant"]
    assert r.messages[3]["content"] == "no matches\n\n" + stuck.nudge_text
    _assert_well_formed(r.messages)
    assert r.user_turns == 1


def test_truncated_reply_replays_as_call_plus_tool_error(tmp_path):
    path = _write(tmp_path, [
        {"kind": "session_start", "session_id": "s8", "model": "m", "mode": "act"},
        {"kind": "user_message", "content": "write it"},
        {"kind": "assistant_message", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "Write", "arguments": "{\"f"}}]},
        {"kind": "tool_result", "tool_call_id": "c1", "name": "Write",
         "content": "cut off", "is_error": True, "error_class": "truncated"},
        {"kind": "truncated_response", "turn": 1, "num_predict": 4096},
        {"kind": "assistant_message", "content": "done", "tool_calls": []},
    ])
    r = rebuild_messages(path, SYS)
    assert r.complete
    assert [m["role"] for m in r.messages] == [
        "system", "user", "assistant", "tool", "assistant"]
    assert r.messages[3]["tool_call_id"] == "c1"
    _assert_well_formed(r.messages)


def test_feedback_message_is_history_not_a_turn(tmp_path):
    path = _write(tmp_path, [
        {"kind": "user_message", "content": "fix it"},
        *_call_records("c1"),
        {"kind": "assistant_message", "content": "done", "tool_calls": []},
        {"kind": "feedback_message", "source": "review", "content": "a reviewer flagged it"},
        {"kind": "assistant_message", "content": "fixed", "tool_calls": []},
    ])
    r = rebuild_messages(path, SYS)
    assert [m["role"] for m in r.messages] == [
        "system", "user", "assistant", "tool", "assistant", "user", "assistant"]
    assert r.messages[5]["content"] == "a reviewer flagged it"
    assert r.user_turns == 1
    _assert_well_formed(r.messages)


def test_mid_turn_compaction_after_feedback_keeps_a_well_formed_tail(tmp_path):
    # Live history before the shrink: s u a t a t a(done) u(feedback); the
    # shrink keeps [s, prompt, summary, a(done), u(feedback)] = 5 messages.
    path = _write(tmp_path, [
        {"kind": "user_message", "content": "the task"},
        *_call_records("c1"),
        *_call_records("c2"),
        {"kind": "assistant_message", "content": "done", "tool_calls": []},
        {"kind": "feedback_message", "source": "done_gate", "content": "checks failed"},
        {"kind": "compaction", "messages_before": 8, "messages_after": 5,
         "summary": "read two files", "mid_turn": True},
        {"kind": "assistant_message", "content": "fixed", "tool_calls": []},
    ])
    r = rebuild_messages(path, SYS)
    assert [m["role"] for m in r.messages] == [
        "system", "user", "assistant", "assistant", "user", "assistant"]
    assert r.messages[1]["content"] == "the task"
    assert r.messages[2]["content"].startswith(SUMMARY_MARKER)
    assert r.messages[4]["content"] == "checks failed"
    assert r.user_turns == 1
    _assert_well_formed(r.messages)
