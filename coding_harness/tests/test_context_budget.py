"""Tests for the context-budget guards: per-tool caps, budget math, Read elision."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from coding_harness.core import context_budget as cb


def _call(cid: str, name: str, **args) -> dict:
    """One assistant tool_call with JSON-encoded arguments."""
    return {"id": cid, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _step(cid: str, name: str, result: str, **args) -> list[dict]:
    """An assistant message with one call plus its tool result."""
    return [
        {"role": "assistant", "content": "", "tool_calls": [_call(cid, name, **args)]},
        {"role": "tool", "tool_call_id": cid, "name": name, "content": result},
    ]


class TestCaps(unittest.TestCase):
    def test_grep_and_glob_capped_to_their_limits(self) -> None:
        for name, cap in (("Grep", 8_000), ("Glob", 4_000)):
            out = cb.clamp_tool_result(name, "a" * 50_000)
            self.assertLessEqual(len(out), cap)
            self.assertIn(f"[{name} result clamped:", out)

    def test_unlisted_tool_gets_default_cap(self) -> None:
        out = cb.clamp_tool_result("mcp__thing", "z" * 20_000)
        self.assertLessEqual(len(out), cb.DEFAULT_CAP)

    def test_read_and_bash_uncapped(self) -> None:
        big = "r" * 50_000
        self.assertEqual(cb.clamp_tool_result("Read", big), big)
        self.assertEqual(cb.clamp_tool_result("Bash", big), big)

    def test_under_cap_untouched(self) -> None:
        self.assertEqual(cb.clamp_tool_result("Glob", "short"), "short")

    def test_clamp_keeps_head_and_tail(self) -> None:
        text = "H" * 10_000 + "T" * 10_000
        out = cb.clamp_tool_result("Grep", text)
        self.assertTrue(out.startswith("H"))
        self.assertTrue(out.endswith("T"))

    def test_kill_switch_disables_clamp(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_CONTEXT_BUDGET": "0"}):
            self.assertFalse(cb.enabled())
            self.assertEqual(len(cb.clamp_tool_result("Grep", "a" * 50_000)), 50_000)


class TestBudget(unittest.TestCase):
    def test_budget_is_sixty_percent_minus_reserve(self) -> None:
        self.assertEqual(cb.token_budget(32_768), int(0.6 * 32_768) - 1_500)
        self.assertEqual(cb.token_budget(10_000), 4_500)

    def test_small_window_budget_has_a_positive_floor(self) -> None:
        self.assertEqual(cb.token_budget(2_048), 512)
        self.assertEqual(cb.token_budget(1_000), 250)

    def test_estimate_counts_messages_and_tools(self) -> None:
        msgs = [{"role": "user", "content": "x" * 4_000}]
        base = cb.estimate_tokens(msgs)
        self.assertGreaterEqual(base, 1_000)
        tools = [{"type": "function", "function": {"name": "T", "description": "d" * 400}}]
        self.assertGreater(cb.estimate_tokens(msgs, tools), base + 90)


def _read(path: str, body: str) -> str:
    """A successful Read result as tools/read.py formats it."""
    return f"# {path} (lines 1-1 of 1)\n     1\t{body}"


class TestElide(unittest.TestCase):
    def _history(self) -> list[dict]:
        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "task"},
            *_step("c1", "Read", _read("/a.py", "old"), file_path="/a.py"),
            *_step("c2", "Read", _read("/b.py", "b"), file_path="/b.py"),
            *_step("c3", "Read", _read("/a.py", "new"), file_path="/a.py"),
        ]

    def test_keeps_latest_read_and_stubs_earlier(self) -> None:
        msgs = self._history()
        self.assertEqual(cb.elide_superseded_reads(msgs), 1)
        self.assertEqual(msgs[3]["content"], cb.ELIDED_TEMPLATE.format(path="/a.py"))
        self.assertEqual(msgs[5]["content"], _read("/b.py", "b"))
        self.assertEqual(msgs[7]["content"], _read("/a.py", "new"))

    def test_idempotent(self) -> None:
        msgs = self._history()
        cb.elide_superseded_reads(msgs)
        self.assertEqual(cb.elide_superseded_reads(msgs), 0)

    def test_edit_supersedes_earlier_read(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            *_step("c1", "Read", _read("/a.py", "x"), file_path="/a.py"),
            *_step("c2", "Edit", "edited /a.py (1 replacement)", file_path="/a.py",
                   old_string="x", new_string="y"),
        ]
        self.assertEqual(cb.elide_superseded_reads(msgs), 1)
        self.assertIn("elided", msgs[2]["content"])

    def test_write_supersedes_earlier_read(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            *_step("c1", "Read", _read("/a.py", "x"), file_path="/a.py"),
            *_step("c2", "Write", "overwrote /a.py (4 bytes, 1 lines)", file_path="/a.py",
                   content="y"),
        ]
        self.assertEqual(cb.elide_superseded_reads(msgs), 1)

    def test_failed_edit_or_read_does_not_supersede(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            *_step("c1", "Read", _read("/a.py", "x"), file_path="/a.py"),
            *_step("c2", "Edit", "error: old_string not found in /a.py", file_path="/a.py",
                   old_string="q", new_string="y"),
            *_step("c3", "Write", "BLOCKED by Sentinel: nope", file_path="/a.py", content="y"),
            *_step("c4", "Read", "error: no such file: /a.py", file_path="/a.py"),
        ]
        self.assertEqual(cb.elide_superseded_reads(msgs), 0)
        self.assertEqual(msgs[2]["content"], _read("/a.py", "x"))

    def test_other_range_of_same_file_is_kept(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            *_step("c1", "Read", _read("/a.py", "1"), file_path="/a.py", offset=1, limit=50),
            *_step("c2", "Read", _read("/a.py", "51"), file_path="/a.py", offset=51, limit=50),
        ]
        self.assertEqual(cb.elide_superseded_reads(msgs), 0)

    def test_multi_call_step_pairs_positionally(self) -> None:
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [
                _call("c1", "Read", file_path="/a.py"),
                _call("c2", "Read", file_path="/b.py"),
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": _read("/a.py", "A1")},
            {"role": "tool", "tool_call_id": "c2", "content": _read("/b.py", "B1")},
            *_step("c3", "Read", _read("/b.py", "B2"), file_path="/b.py"),
        ]
        self.assertEqual(cb.elide_superseded_reads(msgs), 1)
        self.assertEqual(msgs[2]["content"], _read("/a.py", "A1"))
        self.assertIn("/b.py", msgs[3]["content"])

    def test_pending_calls_cleared_by_user_or_plain_reply(self) -> None:
        for interrupter in ({"role": "user", "content": "fix it"},
                            {"role": "assistant", "content": "thinking"}):
            msgs = [
                {"role": "system", "content": "sys"},
                {"role": "assistant", "content": "", "tool_calls": [
                    _call("c1", "Read", file_path="/a.py"),
                    _call("c2", "Read", file_path="/a.py"),
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": _read("/a.py", "A1")},
                interrupter,
                # An unpaired tool message must not inherit the leftover call c2.
                {"role": "tool", "tool_call_id": "zz", "content": _read("/a.py", "A2")},
            ]
            self.assertEqual(cb.elide_superseded_reads(msgs), 0)


if __name__ == "__main__":
    unittest.main()
