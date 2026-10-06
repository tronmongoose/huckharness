"""Tests for auto context compaction.

Pure-module tests drive compaction.compact with a fake chat_fn; the audit
test monkeypatches the chain paths into a tempdir. No Ollama required.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core import compaction
from coding_harness.security import audit


def _fake_chat(**kwargs):
    return {"role": "assistant", "content": "summary of earlier work"}


def _history(n_user_turns: int, pad: int = 0) -> list[dict]:
    """system + n user turns, each followed by a tool-call round trip."""
    msgs: list[dict] = [{"role": "system", "content": "you are a coder"}]
    for i in range(n_user_turns):
        msgs.append({"role": "user", "content": f"task {i} " + "x" * pad})
        msgs.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": f"c{i}", "type": "function",
                "function": {"name": "Read", "arguments": "{}"},
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": f"c{i}", "content": "ok"})
        msgs.append({"role": "assistant", "content": f"done {i}"})
    return msgs


class TestCompactionModule(unittest.TestCase):
    def test_noop_under_threshold(self) -> None:
        msgs = _history(4)
        r = compaction.compact(msgs, model="m", threshold_chars=10**9,
                               chat_fn=_fake_chat)
        self.assertFalse(r.compacted)
        self.assertEqual(r.reason, "under_threshold")
        self.assertIs(r.messages, msgs)

    def test_compacts_over_threshold(self) -> None:
        msgs = _history(6, pad=500)
        r = compaction.compact(msgs, model="m", threshold_chars=100,
                               keep_recent_users=2, chat_fn=_fake_chat)
        self.assertTrue(r.compacted)
        # system prompt survives verbatim
        self.assertEqual(r.messages[0], msgs[0])
        # summary slot is assistant-role and marked
        self.assertEqual(r.messages[1]["role"], "assistant")
        self.assertIn(compaction.SUMMARY_MARKER, r.messages[1]["content"])
        # tail starts at a user boundary (never an orphaned tool message)
        self.assertEqual(r.messages[2]["role"], "user")
        # last 2 user turns kept verbatim
        users = [m for m in r.messages if m.get("role") == "user"]
        self.assertEqual(len(users), 2)
        self.assertEqual(users[0]["content"], msgs[-8]["content"])
        self.assertLess(r.chars_after, r.chars_before)
        # input list untouched
        self.assertEqual(len(msgs), r.messages_before)

    def test_no_orphaned_tool_messages(self) -> None:
        msgs = _history(6, pad=500)
        r = compaction.compact(msgs, model="m", threshold_chars=100,
                               chat_fn=_fake_chat)
        for i, m in enumerate(r.messages):
            if m.get("role") == "tool":
                prev_ids = {
                    c["id"]
                    for c in (r.messages[i - 1].get("tool_calls") or [])
                }
                self.assertIn(m["tool_call_id"], prev_ids)

    def test_too_few_turns_skips(self) -> None:
        msgs = _history(2, pad=5000)
        r = compaction.compact(msgs, model="m", threshold_chars=100,
                               keep_recent_users=2, chat_fn=_fake_chat)
        self.assertFalse(r.compacted)
        self.assertEqual(r.reason, "too_few_turns")

    def test_summarizer_failure_fails_open(self) -> None:
        def _boom(**kwargs):
            raise RuntimeError("ollama down")
        msgs = _history(6, pad=500)
        r = compaction.compact(msgs, model="m", threshold_chars=100,
                               chat_fn=_boom)
        self.assertFalse(r.compacted)
        self.assertIn("summarizer_error", r.reason)
        self.assertIs(r.messages, msgs)

    def test_empty_summary_fails_open(self) -> None:
        def _empty(**kwargs):
            return {"role": "assistant", "content": "   "}
        msgs = _history(6, pad=500)
        r = compaction.compact(msgs, model="m", threshold_chars=100,
                               chat_fn=_empty)
        self.assertFalse(r.compacted)
        self.assertIn("summarizer_error", r.reason)


def _one_turn(steps: int) -> list[dict]:
    """system + one user task + ``steps`` tool-call round trips."""
    msgs: list[dict] = [
        {"role": "system", "content": "you are a coder"},
        {"role": "user", "content": "the task"},
    ]
    for i in range(steps):
        msgs.append({"role": "assistant", "content": None, "tool_calls": [{
            "id": f"s{i}", "type": "function",
            "function": {"name": "Read", "arguments": "{}"},
        }]})
        msgs.append({"role": "tool", "tool_call_id": f"s{i}", "content": "x" * 300})
    return msgs


class TestStepCompaction(unittest.TestCase):
    def test_cut_index_at_last_step(self) -> None:
        msgs = _one_turn(4)
        self.assertEqual(compaction.find_step_cut_index(msgs), len(msgs) - 2)
        self.assertEqual(compaction.find_step_cut_index(msgs, 2), len(msgs) - 4)

    def test_cut_index_none_with_one_step(self) -> None:
        self.assertIsNone(compaction.find_step_cut_index(_one_turn(1)))
        self.assertIsNone(compaction.find_step_cut_index([{"role": "system"}]))

    def test_cut_index_respects_anchor(self) -> None:
        msgs = _history(2) + [{"role": "user", "content": "now"}] + _one_turn(2)[2:]
        anchor = len(_history(2))
        self.assertEqual(compaction.find_step_cut_index(msgs, anchor=anchor), len(msgs) - 2)

    def test_compact_steps_keeps_task_and_last_step(self) -> None:
        msgs = _one_turn(5)
        r = compaction.compact_steps(msgs, model="m", chat_fn=_fake_chat)
        self.assertTrue(r.compacted)
        self.assertEqual(r.reason, "over_budget")
        self.assertEqual(r.messages[0], msgs[0])
        self.assertEqual(r.messages[1], msgs[1])
        self.assertIn(compaction.SUMMARY_MARKER, r.messages[2]["content"])
        self.assertEqual(r.messages[3:], msgs[-2:])
        self.assertEqual(r.messages_after, 5)
        self.assertLess(r.chars_after, r.chars_before)
        self.assertEqual(len(msgs), 12)  # input never mutated

    def test_compact_steps_folds_older_turns_around_anchor(self) -> None:
        older = _history(2)
        msgs = older + [{"role": "user", "content": "now"}] + _one_turn(3)[2:]
        r = compaction.compact_steps(msgs, model="m", anchor=len(older), chat_fn=_fake_chat)
        self.assertTrue(r.compacted)
        self.assertEqual(r.messages[1]["content"], "now")
        self.assertEqual(len(r.messages), 5)

    def test_compact_steps_skips_resummarizing_a_summary(self) -> None:
        msgs = _one_turn(5)
        once = compaction.compact_steps(msgs, model="m", chat_fn=_fake_chat).messages
        again = compaction.compact_steps(once, model="m", chat_fn=_fake_chat)
        self.assertFalse(again.compacted)
        self.assertEqual(again.reason, "too_few_steps")

    def test_compact_steps_fails_open(self) -> None:
        def _boom(**kwargs):
            raise RuntimeError("down")
        msgs = _one_turn(4)
        r = compaction.compact_steps(msgs, model="m", chat_fn=_boom)
        self.assertFalse(r.compacted)
        self.assertIn("summarizer_error", r.reason)
        self.assertIs(r.messages, msgs)

    def test_compact_steps_too_few_steps(self) -> None:
        r = compaction.compact_steps(_one_turn(1), model="m", chat_fn=_fake_chat)
        self.assertEqual(r.reason, "too_few_steps")


class TestCompactionAudit(unittest.TestCase):
    def test_append_compaction_extends_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            patches = [
                mock.patch.object(audit, "META_DIR", tmp_path),
                mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
                mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            ]
            for p in patches:
                p.start()
            try:
                audit.append_compaction(
                    session_id="s1",
                    messages_before=20, messages_after=6,
                    chars_before=200_000, chars_after=30_000,
                    summary="folded",
                )
                ok, detail = audit.verify_chain(tmp_path / "audit.jsonl")
                self.assertTrue(ok, detail)
                entry = (tmp_path / "audit.jsonl").read_text().strip()
                self.assertIn('"kind": "compaction"', entry)
                self.assertIn('"summary_digest"', entry)
                self.assertNotIn("folded", entry)
            finally:
                for p in patches:
                    p.stop()


if __name__ == "__main__":
    unittest.main()
