"""Tests for Plan/Act mode separation.

Covers the three observable contracts:

  1. Tool visibility — Plan mode hides every act-only tool from the model
     via ``ToolRegistry.to_openai_tools``.
  2. Dispatch refusal — even if an act-only tool name slips through (model
     hallucination, test-only direct call), ``ToolRegistry.dispatch``
     refuses and audits the attempt as ``plan_mode_act_only``.
  3. Mode transitions — ``Session.set_mode`` writes a hash-chained
     ``mode_change`` audit entry and synchronises the registry.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.mode import (
    BUILTIN_ACT_ONLY,
    BUILTIN_PLAN_SAFE,
    MCP_PLAN_SAFE_NAMES,
    Mode,
    is_plan_safe,
)
from coding_harness.core.session import Session
from coding_harness.security import audit
from coding_harness.security.sentinel import SentinelVerdict
from coding_harness.tools.bash import Bash
from coding_harness.tools.edit import Edit
from coding_harness.tools.glob_tool import Glob
from coding_harness.tools.grep import Grep
from coding_harness.tools.read import Read
from coding_harness.tools.registry import ToolRegistry
from coding_harness.tools.write import Write


def _build_full_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(Read())
    registry.register(Bash())
    registry.register(Write())
    registry.register(Edit())
    registry.register(Grep())
    registry.register(Glob())
    return registry


class TestModeClassifier(unittest.TestCase):
    def test_builtin_plan_safe(self) -> None:
        for name in ("Read", "Grep", "Glob"):
            self.assertTrue(is_plan_safe(name))
        for name in ("Bash", "Write", "Edit"):
            self.assertFalse(is_plan_safe(name))

    def test_unknown_tool_treated_as_act_only(self) -> None:
        # Fail-closed: anything we haven't classified is hidden in Plan mode.
        self.assertFalse(is_plan_safe("SomeFutureTool"))

    def test_carryall_reads_are_plan_safe(self) -> None:
        for name in (
            "carryall_list_vaults", "carryall_read_document",
            "carryall_query_documents", "carryall_check_access",
            "carryall_get_metadata", "carryall_audit_log",
        ):
            self.assertTrue(is_plan_safe(name), name)

    def test_carryall_writes_are_act_only(self) -> None:
        for name in ("carryall_write_document", "carryall_compile_policy"):
            self.assertFalse(is_plan_safe(name), name)

    def test_classification_sets_are_disjoint(self) -> None:
        self.assertFalse(BUILTIN_PLAN_SAFE & BUILTIN_ACT_ONLY)
        # MCP names should also be disjoint from built-ins.
        self.assertFalse(BUILTIN_PLAN_SAFE & MCP_PLAN_SAFE_NAMES)
        self.assertFalse(BUILTIN_ACT_ONLY & MCP_PLAN_SAFE_NAMES)


class TestRegistryVisibility(unittest.TestCase):
    def test_act_mode_shows_everything(self) -> None:
        registry = _build_full_registry()
        registry.mode = Mode.ACT
        names = {t["function"]["name"] for t in registry.to_openai_tools()}
        self.assertEqual(
            names, {"Read", "Bash", "Write", "Edit", "Grep", "Glob"}
        )

    def test_plan_mode_hides_act_only(self) -> None:
        registry = _build_full_registry()
        registry.mode = Mode.PLAN
        names = {t["function"]["name"] for t in registry.to_openai_tools()}
        self.assertEqual(names, {"Read", "Grep", "Glob"})

    def test_visible_predicate_matches_to_openai_tools(self) -> None:
        registry = _build_full_registry()
        registry.mode = Mode.PLAN
        for name in registry.tools:
            visible = registry._visible(name)
            in_export = any(
                t["function"]["name"] == name
                for t in registry.to_openai_tools()
            )
            self.assertEqual(visible, in_export, name)


class TestPlanModeDispatchBlocks(unittest.TestCase):
    """Even if the model fabricates an act-only tool call from training
    memory, dispatch refuses it in Plan mode and audits the attempt.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.audit_patch = mock.patch.object(
            audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"
        )
        self.anchors_patch = mock.patch.object(
            audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"
        )
        self.meta_patch = mock.patch.object(
            audit, "META_DIR", self.tmp_path
        )
        for p in (self.audit_patch, self.anchors_patch, self.meta_patch):
            p.start()

        self.registry = _build_full_registry()
        self.registry.mode = Mode.PLAN

    def tearDown(self) -> None:
        for p in (self.audit_patch, self.anchors_patch, self.meta_patch):
            p.stop()
        self.tmp.cleanup()

    def test_bash_blocked_in_plan_mode(self) -> None:
        # Sentinel is never reached in this case — visibility gate fires first.
        with mock.patch(
            "coding_harness.tools.registry.sentinel.review",
            mock.MagicMock(return_value=SentinelVerdict(
                allowed=True, reason="should-not-be-called", path="hook"
            )),
        ) as review:
            result = self.registry.dispatch("Bash", {"command": "echo hi"})
            self.assertEqual(review.call_count, 0)
        self.assertTrue(result.is_error)
        self.assertIn("BLOCKED in plan mode", result.content)

        # The block is recorded in the audit chain with the right reason.
        path = audit.AUDIT_PATH
        self.assertTrue(path.exists())
        last = json.loads(path.read_text().strip().splitlines()[-1])
        self.assertEqual(last["tool"], "Bash")
        self.assertFalse(last["allowed"])
        self.assertEqual(last["sentinel_reason"], "plan_mode_act_only")
        self.assertEqual(last["sentinel_path"], "mode")

    def test_read_passes_in_plan_mode(self) -> None:
        # Read is plan-safe — visibility gate lets it through to Sentinel.
        # Run sentinel allow + a real read of this very test file so we
        # exercise the full path without needing a fake tool.
        with mock.patch(
            "coding_harness.tools.registry.sentinel.review",
            mock.MagicMock(return_value=SentinelVerdict(
                allowed=True, reason="approved", path="hook"
            )),
        ):
            result = self.registry.dispatch("Read", {"file_path": __file__})
        self.assertFalse(result.is_error, result.content)


class TestSessionModeTransitions(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.audit_patch = mock.patch.object(
            audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"
        )
        self.anchors_patch = mock.patch.object(
            audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"
        )
        self.meta_patch = mock.patch.object(
            audit, "META_DIR", self.tmp_path
        )
        for p in (self.audit_patch, self.anchors_patch, self.meta_patch):
            p.start()

        self.registry = _build_full_registry()
        # Avoid Snapshotter writing to the real ~/slos by disabling it.
        from coding_harness.security.snapshot import Snapshotter
        self.snapshotter = Snapshotter("test")
        self.snapshotter._dir = self.tmp_path / "snaps"  # private but harmless

        os.environ["CLAWROUTER_FRONTIER_MODEL"] = "claude-sonnet-4-20250514"
        self.session = Session(
            model="dummy",
            registry=self.registry,
            system_prompt="test",
            mode=Mode.ACT,
        )

    def tearDown(self) -> None:
        for p in (self.audit_patch, self.anchors_patch, self.meta_patch):
            p.stop()
        self.tmp.cleanup()

    def test_initial_mode_is_propagated_to_registry(self) -> None:
        self.assertIs(self.session.registry.mode, Mode.ACT)

    def test_set_mode_writes_audit_entry(self) -> None:
        self.session.set_mode(Mode.PLAN, reason="test")
        path = audit.AUDIT_PATH
        last = json.loads(path.read_text().strip().splitlines()[-1])
        self.assertEqual(last["kind"], "mode_change")
        self.assertEqual(last["from_mode"], "act")
        self.assertEqual(last["to_mode"], "plan")
        self.assertEqual(last["reason"], "test")
        # Hash chain is intact.
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)

    def test_set_mode_idempotent(self) -> None:
        self.session.set_mode(Mode.ACT, reason="test")  # already ACT
        path = audit.AUDIT_PATH
        # No entries written for a no-op transition.
        self.assertFalse(path.exists() and path.read_text().strip())

    def test_set_mode_synchronises_registry(self) -> None:
        self.session.set_mode(Mode.PLAN, reason="test")
        self.assertIs(self.session.registry.mode, Mode.PLAN)
        names = {t["function"]["name"]
                 for t in self.session.registry.to_openai_tools()}
        self.assertNotIn("Bash", names)
        self.assertIn("Read", names)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
