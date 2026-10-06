"""Grep and Glob round-trip through Sentinel.

Both tools were implemented in M2-onward but were not gated correctly until
the REPL/cutover work. Before that, calling them through the registry would
short-circuit at ``review()`` with an unknown-tool refusal and the model would
see a refusal with no chance to use the tools. Today the facade classifies
them as read-category built-ins and the in-process policy allows them.

These tests are guard rails. They do not actually invoke ripgrep — only the
sentinel-gating layer.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.security import audit, hook_adapter, policy, sentinel


class GrepGlobRegisteredTests(unittest.TestCase):
    def test_grep_is_a_read_builtin(self) -> None:
        self.assertEqual(sentinel.BUILTIN_CATEGORIES["Grep"], "read")

    def test_glob_is_a_read_builtin(self) -> None:
        self.assertEqual(sentinel.BUILTIN_CATEGORIES["Glob"], "read")


class GrepGlobReviewedNotRefusedTests(unittest.TestCase):
    """Confirm sentinel.review() allows Grep/Glob by category in-process, not
    via the 'unknown category' refusal, and spawns no hook subprocess."""

    def setUp(self) -> None:
        self._patches = [
            patch.object(hook_adapter, "_walk_up", return_value=None),
            patch.dict(hook_adapter._CACHE, clear=True),
            patch.dict("os.environ", {"SENTINEL_GATE_HOOK": ""}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def test_grep_review_allowed_by_category(self) -> None:
        with patch.object(hook_adapter.subprocess, "run") as run_mock:
            verdict = sentinel.review(
                tool_name="Grep",
                tool_input={"pattern": "foo", "path": "/tmp"},
                session_id="t-grep",
            )
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.path, policy.PATH_CATEGORY)
        run_mock.assert_not_called()

    def test_glob_review_allowed_by_category(self) -> None:
        with patch.object(hook_adapter.subprocess, "run") as run_mock:
            verdict = sentinel.review(
                tool_name="Glob",
                tool_input={"pattern": "**/*.py"},
                session_id="t-glob",
            )
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.path, policy.PATH_CATEGORY)
        run_mock.assert_not_called()

    def test_unknown_tool_still_refused(self) -> None:
        # A name with no built-in category and no registry-provided category
        # is refused before any hook could run.
        with patch.object(hook_adapter.subprocess, "run") as run_mock:
            verdict = sentinel.review(
                tool_name="NotARealTool",
                tool_input={},
                session_id="t-bogus",
            )
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.path, policy.PATH_UNKNOWN_CATEGORY)
        run_mock.assert_not_called()


class GrepGlobAuditedThroughRegistryTests(unittest.TestCase):
    """End-to-end through the registry: Grep/Glob produce allowed audit
    entries when the hook approves them. Sentinel + tool body are both
    mocked — we're testing the wiring, not the implementations."""

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

    def test_grep_dispatch_audited_allowed(self) -> None:
        from coding_harness.tools.grep import Grep
        from coding_harness.tools.registry import ToolRegistry

        reg = ToolRegistry(session_id="g1")
        reg.register(Grep())

        with patch.object(
            sentinel, "review",
            return_value=sentinel.SentinelVerdict(
                allowed=True, reason="approved", path="hook",
            ),
        ):
            # Pattern that won't match anything; the tool body runs but result
            # is empty — that's fine, we only care about the audit entry.
            result = reg.dispatch(
                "Grep",
                {"pattern": "zzznope", "path": str(self.tmp_path)},
            )

        self.assertFalse(result.is_error, result.content)
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        chain = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(chain), 1)
        entry = json.loads(chain[0])
        self.assertEqual(entry["tool"], "Grep")
        self.assertTrue(entry["allowed"])

    def test_glob_dispatch_audited_allowed(self) -> None:
        from coding_harness.tools.glob_tool import Glob
        from coding_harness.tools.registry import ToolRegistry

        reg = ToolRegistry(session_id="g2")
        reg.register(Glob())

        with patch.object(
            sentinel, "review",
            return_value=sentinel.SentinelVerdict(
                allowed=True, reason="approved", path="hook",
            ),
        ):
            result = reg.dispatch(
                "Glob",
                {"pattern": "*.nothing", "path": str(self.tmp_path)},
            )

        self.assertFalse(result.is_error, result.content)
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        chain = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(chain), 1)
        entry = json.loads(chain[0])
        self.assertEqual(entry["tool"], "Glob")
        self.assertTrue(entry["allowed"])


if __name__ == "__main__":
    unittest.main()
