"""Sentinel middleware: every dispatch is gated; bypass attempts fail.

The registry tests patch ``security.sentinel.review`` and assert the registry
honors its verdict. The facade tests pin the two paths behind ``review``: the
default in-process policy spawns no subprocess, and ``HARNESS_POLICY=hook``
runs only the legacy subprocess hook.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from coding_harness.security import audit, hook_adapter, policy, sentinel
from coding_harness.tools.base import Tool, ToolResult
from coding_harness.tools.bash import Bash
from coding_harness.tools.edit import Edit
from coding_harness.tools.glob_tool import Glob
from coding_harness.tools.grep import Grep
from coding_harness.tools.read import Read
from coding_harness.tools.registry import ToolRegistry
from coding_harness.tools.write import Write


class _Spy(Tool):
    name = "Read"  # use a real gate name so the registry doesn't refuse it
    description = "spy"
    parameters = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls = 0

    def run(self, args):  # type: ignore[no-untyped-def]
        self.calls += 1
        return ToolResult(content="ran")


class _FakeProc:
    def __init__(self, returncode: int, stderr: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = ""


class SentinelMiddlewareTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._audit_patches = [
            patch.object(audit, "META_DIR", self.tmp_path),
            patch.object(audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"),
            patch.object(audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"),
        ]
        for p in self._audit_patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._audit_patches:
            p.stop()
        self.tmp.cleanup()

    def test_blocked_verdict_prevents_tool_run(self) -> None:
        spy = _Spy()
        reg = ToolRegistry(session_id="t1")
        reg.register(spy)

        with patch.object(
            sentinel, "review",
            return_value=sentinel.SentinelVerdict(
                allowed=False, reason="test_block", path="fast",
            ),
        ):
            result = reg.dispatch("Read", {"file_path": "/tmp/x"})

        self.assertEqual(spy.calls, 0)
        self.assertTrue(result.is_error)
        self.assertIn("BLOCKED", result.content)
        self.assertIn("test_block", result.content)

        # Audit chain should still have one entry, marked not allowed.
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        chain = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(chain), 1)
        self.assertIn('"allowed": false', chain[0])

    def test_allowed_verdict_runs_tool_and_audits(self) -> None:
        spy = _Spy()
        reg = ToolRegistry(session_id="t2")
        reg.register(spy)

        with patch.object(
            sentinel, "review",
            return_value=sentinel.SentinelVerdict(
                allowed=True, reason="approved", path="hook",
            ),
        ):
            result = reg.dispatch("Read", {"file_path": "/tmp/y"})

        self.assertEqual(spy.calls, 1)
        self.assertFalse(result.is_error)
        self.assertEqual(result.content, "ran")

        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        chain = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(chain), 1)
        self.assertIn('"allowed": true', chain[0])

    def test_unknown_tool_audited_and_refused_without_calling_sentinel(self) -> None:
        reg = ToolRegistry(session_id="t3")
        # Note: no tools registered.

        sentinel_calls: list[tuple[str, dict, str]] = []

        def fake_review(tool_name, tool_input, session_id, **kwargs):  # type: ignore[no-untyped-def]
            sentinel_calls.append((tool_name, tool_input, session_id))
            return sentinel.SentinelVerdict(allowed=True, reason="should not run", path="hook")

        with patch.object(sentinel, "review", side_effect=fake_review):
            result = reg.dispatch("Read", {"file_path": "/tmp/x"})

        self.assertEqual(sentinel_calls, [],
                         "sentinel must not be called for unregistered tools")
        self.assertTrue(result.is_error)
        self.assertIn("unknown tool", result.content)

        # And the refusal is on the audit chain.
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        self.assertEqual(
            len((self.tmp_path / "audit.jsonl").read_text().strip().splitlines()),
            1,
        )

    def test_tool_exception_is_caught_and_audited(self) -> None:
        class Exploder(Tool):
            name = "Read"
            description = "boom"
            parameters = {"type": "object", "properties": {}}

            def run(self, args):  # type: ignore[no-untyped-def]
                raise RuntimeError("kaboom")

        reg = ToolRegistry(session_id="t4")
        reg.register(Exploder())

        with patch.object(
            sentinel, "review",
            return_value=sentinel.SentinelVerdict(
                allowed=True, reason="approved", path="hook",
            ),
        ):
            result = reg.dispatch("Read", {})

        self.assertTrue(result.is_error)
        self.assertIn("RuntimeError", result.content)

        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)

    def test_registry_passes_category_and_cwd(self) -> None:
        reg = ToolRegistry(session_id="t5")
        reg.register(_Spy())
        review = MagicMock(
            return_value=sentinel.SentinelVerdict(allowed=True, reason="ok", path="x"),
        )
        with patch.object(sentinel, "review", review):
            reg.dispatch("Read", {"file_path": "/tmp/x"})
        kwargs = review.call_args.kwargs
        self.assertEqual(kwargs["category"], "read")
        self.assertEqual(kwargs["cwd"], os.getcwd())
        self.assertEqual(kwargs["tool_name"], "Read")


    def test_default_dispatch_spawns_no_subprocess(self) -> None:
        spy = _Spy()
        reg = ToolRegistry(session_id="t6")
        reg.register(spy)
        hermetic = [
            patch.dict(os.environ, {"SENTINEL_GATE_HOOK": "", "HARNESS_POLICY": ""}),
            patch.dict(hook_adapter._CACHE, clear=True),
            patch.object(hook_adapter, "_walk_up", return_value=None),
            patch.object(
                hook_adapter.subprocess, "run",
                side_effect=AssertionError("default policy path spawned a subprocess"),
            ),
        ]
        for p in hermetic:
            p.start()
        try:
            result = reg.dispatch("Read", {"file_path": "/tmp/x"})
        finally:
            for p in reversed(hermetic):
                p.stop()
        self.assertFalse(result.is_error, result.content)
        self.assertEqual(spy.calls, 1)
        chain = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(json.loads(chain[0])["sentinel_path"], policy.PATH_CATEGORY)


class SentinelFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        self._patches = [
            patch.dict(os.environ, {"SENTINEL_GATE_HOOK": ""}),
            patch.dict(hook_adapter._CACHE, clear=True),
            patch.object(hook_adapter, "_walk_up", return_value=None),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def test_default_path_is_in_process_with_no_subprocess(self) -> None:
        with patch.object(hook_adapter.subprocess, "run") as run_mock:
            allowed = sentinel.review("Bash", {"command": "ls"}, "s")
            denied = sentinel.review("Bash", {"command": "sudo ls"}, "s")
        run_mock.assert_not_called()
        self.assertTrue(allowed.allowed)
        self.assertEqual(allowed.path, policy.PATH_CATEGORY)
        self.assertFalse(denied.allowed)
        self.assertEqual(denied.path, policy.PATH_BLOCKLIST)

    def test_builtin_category_defaults_when_registry_gives_none(self) -> None:
        verdict = sentinel.review("Write", {"file_path": "/tmp/x", "content": ""}, "s")
        self.assertTrue(verdict.allowed)
        verdict = sentinel.review("NotATool", {}, "s")
        self.assertEqual(verdict.path, policy.PATH_UNKNOWN_CATEGORY)

    def test_hook_mode_runs_only_the_subprocess_hook(self) -> None:
        def fake_run(cmd, input, text, capture_output, timeout):  # type: ignore[no-untyped-def]
            return _FakeProc(returncode=0)

        with patch.dict(os.environ, {"HARNESS_POLICY": "hook"}), patch.object(
            hook_adapter.subprocess, "run", side_effect=fake_run,
        ) as run_mock:
            # The in-process blocklist would deny this; hook mode must not run it.
            verdict = sentinel.review("Bash", {"command": "sudo ls"}, "s")
        run_mock.assert_called_once()
        cmd = run_mock.call_args.args[0]
        self.assertEqual(cmd, [sys.executable, str(hook_adapter._BUNDLED)])
        payload = json.loads(run_mock.call_args.kwargs["input"])
        self.assertEqual(payload["tool_name"], "Bash")
        self.assertEqual(payload["category"], "execute")
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.path, policy.PATH_HOOK)

    def test_hook_mode_without_any_hook_fails_closed(self) -> None:
        with patch.dict(os.environ, {"HARNESS_POLICY": "hook"}), patch.object(
            hook_adapter, "_BUNDLED", Path("/nonexistent/sentinel-gate.py"),
        ):
            verdict = sentinel.review("Read", {"file_path": "/tmp/x"}, "s")
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.path, policy.PATH_HOOK)


class BuiltinCategoryTests(unittest.TestCase):
    def test_builtin_tools_declare_the_facade_category(self) -> None:
        expected = {
            Read: "read", Grep: "read", Glob: "read",
            Write: "edit", Edit: "edit", Bash: "execute",
        }
        for cls, category in expected.items():
            with self.subTest(tool=cls.name):
                self.assertEqual(cls.category, category)
                self.assertEqual(sentinel.BUILTIN_CATEGORIES[cls.name], category)
        self.assertEqual(Tool.category, "read")


if __name__ == "__main__":
    unittest.main()
