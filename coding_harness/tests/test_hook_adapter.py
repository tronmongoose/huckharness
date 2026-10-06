"""Deployment-hook adapter: exit codes, fail-closed timeout, resolution walk.

Real subprocesses against tiny throwaway hook scripts so the stdin-JSON /
exit-2 contract is exercised end to end with the harness's own interpreter.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.security import hook_adapter


def _write_hook(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("import json, sys, time\n" + body)
    return path


class _AdapterCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self._env = patch.dict(os.environ, {"SENTINEL_GATE_HOOK": ""})
        self._env.start()
        self._cache = patch.dict(hook_adapter._CACHE, clear=True)
        self._cache.start()

    def tearDown(self) -> None:
        self._cache.stop()
        self._env.stop()
        self.tmp.cleanup()

    def review(self, hook: Path, **kwargs):  # type: ignore[no-untyped-def]
        with patch.dict(os.environ, {"SENTINEL_GATE_HOOK": str(hook)}):
            return hook_adapter.review(
                "Bash", "execute", {"command": "ls"}, "sess", cwd=str(self.root), **kwargs,
            )


class TestExitContract(_AdapterCase):
    def test_exit_two_blocks_with_stderr_reason(self) -> None:
        hook = _write_hook(
            self.root / "deny.py",
            "print('vault rule hit', file=sys.stderr); sys.exit(2)\n",
        )
        verdict = self.review(hook)
        self.assertIsNotNone(verdict)
        self.assertFalse(verdict.allowed)
        self.assertIn("vault rule hit", verdict.reason)

    def test_exit_zero_allows(self) -> None:
        hook = _write_hook(self.root / "allow.py", "sys.exit(0)\n")
        verdict = self.review(hook)
        self.assertTrue(verdict.allowed)

    def test_other_exit_code_fails_closed(self) -> None:
        hook = _write_hook(self.root / "crash.py", "raise RuntimeError('boom')\n")
        verdict = self.review(hook)
        self.assertFalse(verdict.allowed)
        self.assertIn("unexpected exit", verdict.reason)

    def test_timeout_fails_closed(self) -> None:
        hook = _write_hook(self.root / "slow.py", "time.sleep(30); sys.exit(0)\n")
        with patch.object(hook_adapter, "TIMEOUT_S", 0.5):
            verdict = self.review(hook)
        self.assertFalse(verdict.allowed)
        self.assertIn("timed out", verdict.reason)

    def test_missing_configured_hook_fails_closed(self) -> None:
        verdict = self.review(self.root / "does-not-exist.py")
        self.assertIsNotNone(verdict)
        self.assertFalse(verdict.allowed)
        self.assertIn("missing", verdict.reason)

    def test_event_carries_category_and_real_tool_name(self) -> None:
        capture = self.root / "event.json"
        hook = _write_hook(
            self.root / "spy.py",
            f"open({str(capture)!r}, 'w').write(sys.stdin.read()); sys.exit(0)\n",
        )
        with patch.dict(os.environ, {"SENTINEL_GATE_HOOK": str(hook)}):
            hook_adapter.review(
                "carryall_read_document", "read", {"id": 1}, "sess-9", cwd=str(self.root),
            )
        event = json.loads(capture.read_text())
        self.assertEqual(event["tool_name"], "carryall_read_document")
        self.assertEqual(event["category"], "read")
        self.assertEqual(event["session_id"], "sess-9")
        self.assertEqual(event["tool_input"], {"id": 1})


class TestResolution(_AdapterCase):
    def test_no_hook_above_cwd_returns_none(self) -> None:
        with patch.object(hook_adapter, "_BUNDLED", self.root / "nowhere.py"):
            self.assertIsNone(hook_adapter.resolve(str(self.root / "a" / "b")))
            verdict = hook_adapter.review(
                "Bash", "execute", {"command": "ls"}, "s", cwd=str(self.root),
            )
        self.assertIsNone(verdict)

    def test_deployment_hook_found_by_walking_up(self) -> None:
        deploy = _write_hook(self.root / hook_adapter.HOOK_REL, "sys.exit(0)\n")
        nested = self.root / "work" / "tree"
        nested.mkdir(parents=True)
        with patch.object(hook_adapter, "_BUNDLED", self.root / "nowhere.py"):
            self.assertEqual(hook_adapter.resolve(str(nested)), deploy)

    def test_bundled_hook_skipped_during_walk(self) -> None:
        bundled = _write_hook(self.root / hook_adapter.HOOK_REL, "sys.exit(2)\n")
        nested = self.root / "pkg" / "sub"
        nested.mkdir(parents=True)
        with patch.object(hook_adapter, "_BUNDLED", bundled.resolve()):
            self.assertIsNone(hook_adapter.resolve(str(nested)))
            self.assertEqual(
                hook_adapter.resolve(str(nested), allow_bundled=True), bundled.resolve(),
            )

    def test_env_override_beats_walk(self) -> None:
        _write_hook(self.root / hook_adapter.HOOK_REL, "sys.exit(0)\n")
        override = _write_hook(self.root / "override.py", "sys.exit(2)\n")
        with patch.dict(os.environ, {"SENTINEL_GATE_HOOK": str(override)}):
            self.assertEqual(hook_adapter.resolve(str(self.root)), override)

    def test_resolution_cached_per_cwd(self) -> None:
        with patch.object(hook_adapter, "_BUNDLED", self.root / "nowhere.py"):
            with patch.object(hook_adapter, "_walk_up", return_value=None) as walk:
                hook_adapter.resolve(str(self.root))
                hook_adapter.resolve(str(self.root))
                hook_adapter.resolve(str(self.root / "other"))
        self.assertEqual(walk.call_count, 2)


if __name__ == "__main__":
    unittest.main()
