"""Composer routes: GET /v1/files, GET /v1/commands, and @path expansion.

Unit tests run ``serve_composer`` against a throwaway git repo; the route
tests reuse the real-server harness from test_serve_mode.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.modes import serve_composer
from coding_harness.security import audit
from coding_harness.tests.test_serve_mode import (
    _fake_ollama_chat,
    _request,
    _start_server,
)


def _git_repo(root: Path, files: list[str]) -> None:
    """A git repo at ``root`` with ``files`` created and tracked."""
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)


class TestComposerHelpers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        serve_composer._cache.clear()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        serve_composer._cache.clear()

    def test_subsequence_filter_is_case_insensitive(self) -> None:
        _git_repo(self.root, ["src/App.tsx", "src/lib/api.ts", "README.md"])
        files = serve_composer.list_files(str(self.root), "sapp")["files"]
        self.assertEqual(files, ["src/App.tsx"])
        files = serve_composer.list_files(str(self.root), "RDM")["files"]
        self.assertEqual(files, ["README.md"])
        self.assertEqual(serve_composer.list_files(str(self.root), "zzz")["files"], [])

    def test_caps_at_fifty(self) -> None:
        _git_repo(self.root, [f"pkg/mod_{i:03d}.py" for i in range(80)])
        files = serve_composer.list_files(str(self.root), "mod")["files"]
        self.assertEqual(len(files), serve_composer.MAX_FILES)

    def test_outside_git_is_empty(self) -> None:
        self.assertEqual(serve_composer.list_files(str(self.root), "")["files"], [])

    def test_mention_expands_only_existing_files_under_cwd(self) -> None:
        _git_repo(self.root, ["src/a.py", "b.md"])
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("no")
        self.addCleanup(outside.unlink)
        (self.root / "link.txt").symlink_to(outside)
        msg = (f"look at @src/a.py, and @b.md. not @missing.py nor @../x "
               f"nor @{outside} nor @link.txt nor me@src/a.py; again @src/a.py")
        line = serve_composer.mention_line(msg, str(self.root))
        self.assertEqual(line, "Attached files: src/a.py, b.md")

    def test_mention_attaches_the_normalized_path(self) -> None:
        _git_repo(self.root, ["src/a.py"])
        line = serve_composer.mention_line("see @./src/../src/a.py and @src/a.py", str(self.root))
        self.assertEqual(line, "Attached files: src/a.py")

    def test_picker_drops_missing_and_escaping_paths(self) -> None:
        _git_repo(self.root, ["keep.py", "gone.py"])
        outside = Path(tempfile.mkdtemp()) / "secret.txt"
        outside.write_text("no")
        self.addCleanup(outside.unlink)
        (self.root / "link.txt").symlink_to(outside)
        subprocess.run(["git", "add", "link.txt"], cwd=self.root, check=True)
        (self.root / "gone.py").unlink()
        self.assertEqual(serve_composer.list_files(str(self.root), "")["files"], ["keep.py"])

    def test_bare_slash_passes_through(self) -> None:
        for msg in ("/", "/ ", "/\n", "/  \t"):
            self.assertEqual(serve_composer.expand_custom(msg, str(self.root)), msg)

    def test_no_mentions_no_line(self) -> None:
        self.assertEqual(serve_composer.mention_line("plain text", str(self.root)), "")

    def test_commands_list_builtins_custom_and_skills(self) -> None:
        _git_repo(self.root, [])
        cmd_dir = self.root / ".bjorn" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "ship.md").write_text("Ship it: $ARGUMENTS\n")
        with mock.patch.object(serve_composer.custom_commands, "COMMANDS_USER",
                               self.root / "none"):
            rows = serve_composer.list_commands(str(self.root))["commands"]
        names = [r["name"] for r in rows]
        self.assertEqual(names[:6], ["/model", "/new", "/clear", "/compact", "/stop", "/skill"])
        ship = next(r for r in rows if r["name"] == "/ship")
        self.assertEqual((ship["kind"], ship["help"]), ("custom", "Ship it: $ARGUMENTS"))
        self.assertTrue(all(r["help"] is not None for r in rows))
        self.assertTrue(all(r["name"].startswith("/skill ") for r in rows if r["kind"] == "skill"))
        expanded = serve_composer.expand_custom("/ship v2", str(self.root))
        self.assertEqual(expanded, "Ship it: v2\n")
        self.assertEqual(serve_composer.expand_custom("/model x", str(self.root)), "/model x")


class TestComposerRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch("coding_harness.core.session.ollama.chat",
                       side_effect=_fake_ollama_chat),
            mock.patch("coding_harness.tools.registry.sentinel.review",
                       return_value=mock.MagicMock(allowed=True, reason="t", path="hook")),
        ]
        for p in cls.patches:
            p.start()
        cls.server, cls.thread, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def test_files_route(self) -> None:
        status, body = _request("GET", self._url("/v1/files?q=serve_composer"))
        self.assertEqual(status, 200)
        files = json.loads(body)["files"]
        self.assertIn("coding_harness/modes/serve_composer.py", files)
        self.assertLessEqual(len(files), serve_composer.MAX_FILES)

    def test_commands_route(self) -> None:
        status, body = _request("GET", self._url("/v1/commands"))
        self.assertEqual(status, 200)
        rows = json.loads(body)["commands"]
        self.assertEqual(rows[0]["name"], "/model")
        self.assertEqual(rows[0]["kind"], "builtin")

    def test_turn_with_bare_slash_answers(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/turn"),
                                body={"message": "/ ", "wait": True})
        self.assertEqual(status, 200, body)

    def test_turn_names_mentioned_files(self) -> None:
        self.assertTrue(Path(os.getcwd(), "README.md").is_file())
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/turn"), body={
            "message": "read @README.md and @nope.md", "wait": True})
        self.assertEqual(status, 200, body)
        text = json.loads(body)["text"]
        self.assertIn("Attached files: README.md", text)
        self.assertNotIn("nope.md,", text)


if __name__ == "__main__":
    unittest.main()
