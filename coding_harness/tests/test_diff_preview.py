"""Tests for the plan/confirm/apply gate added to write-class tools.

Confirmation runs at the *registry* level, not inside the tool. These tests
patch ``security.sentinel.review`` to allow everything (so we test the new
gate, not Sentinel's behavior) and assert:

* No confirm callback → registry chains plan → apply (back-compat).
* Callback returns False → no disk write, audit shows ``denied_by_operator``.
* Callback returns True  → disk written, audit shows ``denied_by_operator=False``.
* Sentinel block fires before confirm — callback is never invoked.
* Print mode ``--dry-run`` denies all writes; the diff lands in the result.
* Validation errors in plan() are returned as ToolResult, never reach confirm.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.security import audit, sentinel
from coding_harness.tools.base import WritePlan
from coding_harness.tools.edit import Edit
from coding_harness.tools.registry import ToolRegistry
from coding_harness.tools.write import Write


def _allow_all():
    return patch.object(
        sentinel, "review",
        return_value=sentinel.SentinelVerdict(allowed=True, reason="ok", path="hook"),
    )


class DiffPreviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patches = [
            patch.object(audit, "META_DIR", self.tmp_path),
            patch.object(audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"),
            patch.object(audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _registry(self, *, confirm=None) -> ToolRegistry:
        reg = ToolRegistry(session_id="diff-test", confirm_callback=confirm)
        reg.register(Write())
        reg.register(Edit())
        return reg

    # ── Plan/apply correctness ───────────────────────────────────────

    def test_no_callback_writes_apply_path_through_run(self) -> None:
        target = self.tmp_path / "out.txt"
        with _allow_all():
            reg = self._registry()
            result = reg.dispatch("Write", {
                "file_path": str(target),
                "content": "hello\n",
            })
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(target.read_text(), "hello\n")

    def test_validation_error_in_plan_returns_tool_error(self) -> None:
        # Write expects an absolute path. Plan must reject without invoking
        # any confirm callback.
        called = []

        def cb(plan):
            called.append(plan)
            return True

        with _allow_all():
            reg = self._registry(confirm=cb)
            result = reg.dispatch("Write", {
                "file_path": "relative.txt",
                "content": "x",
            })
        self.assertTrue(result.is_error)
        self.assertEqual(called, [])

    # ── Deny path ────────────────────────────────────────────────────

    def test_deny_callback_skips_apply_and_audits_denied(self) -> None:
        target = self.tmp_path / "denied.txt"
        denied = []

        def cb(plan: WritePlan) -> bool:
            denied.append(plan)
            return False

        with _allow_all():
            reg = self._registry(confirm=cb)
            result = reg.dispatch("Write", {
                "file_path": str(target),
                "content": "should not be written",
            })

        self.assertEqual(len(denied), 1)
        self.assertFalse(target.exists(), "file should not be written on deny")
        self.assertFalse(result.is_error)
        self.assertIn("denied by operator", result.content)
        self.assertTrue(result.metadata.get("denied_by_operator"))

        # Audit chain should have one entry, allowed=True, denied_by_operator=True.
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        chain = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(chain), 1)
        self.assertIn('"allowed": true', chain[0])
        self.assertIn('"denied_by_operator": true', chain[0])

    def test_accept_callback_runs_apply(self) -> None:
        target = self.tmp_path / "accepted.txt"
        seen = []

        def cb(plan: WritePlan) -> bool:
            seen.append(plan.unified_diff)
            return True

        with _allow_all():
            reg = self._registry(confirm=cb)
            result = reg.dispatch("Write", {
                "file_path": str(target),
                "content": "ok\n",
            })

        self.assertFalse(result.is_error, result.content)
        self.assertEqual(target.read_text(), "ok\n")
        self.assertEqual(len(seen), 1)
        self.assertIn("ok", seen[0])  # diff body contains the new line

        chain = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertIn('"denied_by_operator": false', chain[0])

    # ── Sentinel precedes confirm ────────────────────────────────────

    def test_sentinel_block_skips_confirm(self) -> None:
        target = self.tmp_path / "blocked.txt"
        called = []

        def cb(plan):
            called.append(plan)
            return True

        reg = self._registry(confirm=cb)
        with patch.object(
            sentinel, "review",
            return_value=sentinel.SentinelVerdict(
                allowed=False, reason="test_block", path="fast",
            ),
        ):
            result = reg.dispatch("Write", {
                "file_path": str(target),
                "content": "blocked",
            })

        self.assertTrue(result.is_error)
        self.assertIn("BLOCKED", result.content)
        self.assertEqual(called, [], "confirm callback must not run after Sentinel block")
        self.assertFalse(target.exists())

    # ── Diff content ─────────────────────────────────────────────────

    def test_plan_diff_for_existing_file_shows_unified_diff(self) -> None:
        target = self.tmp_path / "edit.py"
        target.write_text("def hi():\n    print('hi')\n")
        plan_seen = []

        def cb(plan: WritePlan) -> bool:
            plan_seen.append(plan)
            return False  # don't actually apply

        with _allow_all():
            reg = self._registry(confirm=cb)
            reg.dispatch("Edit", {
                "file_path": str(target),
                "old_string": "print('hi')",
                "new_string": "print('hello')",
            })

        self.assertEqual(len(plan_seen), 1)
        plan = plan_seen[0]
        self.assertTrue(plan.existed)
        self.assertIn("-    print('hi')", plan.unified_diff)
        self.assertIn("+    print('hello')", plan.unified_diff)

        # File must remain untouched.
        self.assertEqual(target.read_text(), "def hi():\n    print('hi')\n")

    def test_plan_diff_for_new_file_synthesizes_new_file_marker(self) -> None:
        target = self.tmp_path / "brand_new.txt"
        plan_seen = []

        def cb(plan: WritePlan) -> bool:
            plan_seen.append(plan)
            return False

        with _allow_all():
            reg = self._registry(confirm=cb)
            reg.dispatch("Write", {
                "file_path": str(target),
                "content": "first line\nsecond line\n",
            })

        self.assertEqual(len(plan_seen), 1)
        plan = plan_seen[0]
        self.assertFalse(plan.existed)
        self.assertIn("/dev/null", plan.unified_diff)
        self.assertIn("+first line", plan.unified_diff)
        self.assertIn("+second line", plan.unified_diff)

    def test_planned_diff_headers_are_relative_to_cwd(self) -> None:
        sub = self.tmp_path / "pkg"
        sub.mkdir()
        target = sub / "mod.py"
        target.write_text("x = 1\n")
        with patch("os.getcwd", return_value=str(self.tmp_path.resolve())):
            edit = Edit().plan({"file_path": str(target.resolve()),
                                "old_string": "x = 1", "new_string": "x = 2"})
            new = Write().plan({"file_path": str((sub / "new.py").resolve()), "content": "y\n"})
            outside = Write().plan({"file_path": "/elsewhere/z.py", "content": "z\n"})
        self.assertIsInstance(edit, WritePlan)
        self.assertIn("--- a/pkg/mod.py\n+++ b/pkg/mod.py", edit.unified_diff)
        self.assertIn("+++ b/pkg/new.py", new.unified_diff)
        self.assertNotIn(str(self.tmp_path.resolve()), edit.unified_diff + new.unified_diff)
        self.assertIn("+++ b/elsewhere/z.py", outside.unified_diff)


class PrintModeDryRunTests(unittest.TestCase):
    """The print-mode --dry-run callback is just _dry_run_deny — but we test
    it through the registry to cover the wiring path users exercise on the
    CLI."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patches = [
            patch.object(audit, "META_DIR", self.tmp_path),
            patch.object(audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"),
            patch.object(audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def test_dry_run_returns_diff_in_result_metadata(self) -> None:
        from coding_harness.modes.print_mode import _dry_run_deny

        target = self.tmp_path / "dry.txt"
        reg = ToolRegistry(session_id="dry-test", confirm_callback=_dry_run_deny)
        reg.register(Write())

        with _allow_all():
            result = reg.dispatch("Write", {
                "file_path": str(target),
                "content": "should not land",
            })

        self.assertFalse(target.exists())
        self.assertFalse(result.is_error)
        self.assertIn("denied by operator", result.content)
        self.assertIn("/dev/null", result.metadata.get("diff", ""))


if __name__ == "__main__":
    unittest.main()
