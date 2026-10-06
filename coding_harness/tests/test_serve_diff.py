"""Serve routes for the GUI changes view: diff, revert-file, rewind, review gate.

A real ``ThreadingHTTPServer`` runs with the process cwd moved into a temp
work tree, so the session's shadow checkpoints cover only that tree. The fake
model answers a ``write <name> <text>`` prompt with one Write call and then a
final message, so every turn is deterministic.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.session import SESSIONS_DIR
from coding_harness.security import audit, snapshot
from coding_harness.tests.test_serve_mode import _request, _start_server

_WRITE = re.compile(r"^write (\S+) (.*)$")
# Held by a test to keep the fake model from answering, so a turn stays in flight.
_GATE = threading.Event()
_GATE.set()


def _fake_chat(*, model, messages, tools, on_delta=None, timeout=None, **_kw):
    """Write the named file on a fresh prompt; finish once a tool result is back."""
    _GATE.wait(10)
    last = messages[-1]
    match = _WRITE.match(str(last.get("content", ""))) if last.get("role") == "user" else None
    usage = {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None}
    if match is None:
        return {"role": "assistant", "content": "done", "tool_calls": [], "_usage": usage}
    args = {"file_path": os.path.join(os.getcwd(), match.group(1)), "content": match.group(2) + "\n"}
    call = {"id": "c1", "function": {"name": "Write", "arguments": json.dumps(args)}}
    return {"role": "assistant", "content": "", "tool_calls": [call], "_usage": usage}


class ServeDiffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.meta = Path(cls.tmp.name) / "meta"
        cls.work = Path(cls.tmp.name) / "work"
        cls.work.mkdir()
        cls.old_cwd = os.getcwd()
        os.chdir(cls.work)
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", cls.meta / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", cls.meta / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", cls.meta),
            mock.patch.dict(os.environ, {"HARNESS_REVIEW": "0", "HARNESS_VERIFY_REPAIR": "0"}),
            mock.patch("coding_harness.core.session.ollama.chat", side_effect=_fake_chat),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(allowed=True, reason="test", path="hook"),
            ),
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
        os.chdir(cls.old_cwd)
        cls.tmp.cleanup()

    def _url(self, path: str) -> str:
        """Absolute URL on the test server."""
        return f"http://127.0.0.1:{self.port}{path}"

    def _session(self) -> str:
        """A fresh interactive Act session."""
        status, body = _request("POST", self._url("/v1/sessions"),
                                body={"mode": "act", "interactive": True, "autonomy": "medium"})
        self.assertEqual(status, 201, body)
        return json.loads(body)["session_id"]

    def _turn(self, sid: str, message: str) -> dict:
        """Run one turn to completion."""
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/turn"),
                                body={"message": message, "wait": True}, timeout=30)
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def _call(self, method: str, sid: str, sub: str, body: dict | None = None) -> tuple[int, dict]:
        """One JSON call on a session route."""
        status, raw = _request(method, self._url(f"/v1/sessions/{sid}/{sub}"), body=body, timeout=30)
        return status, json.loads(raw)

    def test_diff_lists_added_file(self) -> None:
        sid = self._session()
        self._turn(sid, "write added.txt hello")
        status, payload = self._call("GET", sid, "diff?turn=1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["turn"], 1)
        self.assertTrue(payload["base"])
        by_path = {f["path"]: f for f in payload["files"]}
        self.assertEqual(by_path["added.txt"]["status"], "added")
        self.assertIn("+hello", by_path["added.txt"]["diff"])
        status, whole = self._call("GET", sid, "diff")
        self.assertEqual(status, 200)
        self.assertIsNone(whole["turn"])

    def test_revert_file_restores_bytes_and_forgets_snapshot(self) -> None:
        sid = self._session()
        target = self.work / "keep.txt"
        target.write_bytes(b"original\n")
        self._turn(sid, "write keep.txt changed")
        self.assertEqual(target.read_text(), "changed\n")
        turn_dir = snapshot.DEFAULT_ROOT / sid / "turn_0001"
        self.assertTrue(list(turn_dir.glob("*.manifest.json")))
        status, payload = self._call("POST", sid, "revert-file", {"path": "keep.txt", "turn": 1})
        self.assertEqual(status, 200, payload)
        self.assertEqual(target.read_bytes(), b"original\n")
        self.assertEqual(list(turn_dir.glob("*.manifest.json")), [])
        _, diff = self._call("GET", sid, "diff?turn=1")
        self.assertNotIn("keep.txt", [f["path"] for f in diff["files"]])

    def test_rewind_restores_disk_and_history(self) -> None:
        sid = self._session()
        self._turn(sid, "write r1.txt one")
        self._turn(sid, "write r2.txt two")
        status, payload = self._call("POST", sid, "rewind", {"turns": 1})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["user_turn"], 1)
        self.assertIn("r2.txt", payload["shadow_removed"])
        self.assertFalse((self.work / "r2.txt").exists())
        self.assertTrue((self.work / "r1.txt").exists())
        _, diff = self._call("GET", sid, "diff")
        self.assertEqual([f["path"] for f in diff["files"]], ["r1.txt"])

    def test_routes_409_mid_turn_and_404_unknown(self) -> None:
        sid = self._session()
        _GATE.clear()
        try:
            status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/turn"), body={"message": "hi"})
            self.assertEqual(status, 202)
            self.assertEqual(self._call("GET", sid, "diff")[0], 409)
            self.assertEqual(self._call("POST", sid, "rewind", {"turns": 1})[0], 409)
            self.assertEqual(self._call("POST", sid, "revert-file", {"path": "x", "turn": 1})[0], 409)
        finally:
            _GATE.set()
        self.assertEqual(self._call("GET", "nope", "diff")[0], 404)
        self.assertEqual(self._call("POST", "nope", "rewind", {"turns": 1})[0], 404)
        self.assertEqual(self._call("POST", sid, "review", {"enabled": "yes"})[0], 400)

    def _pending(self, sid: str) -> list[dict]:
        """Poll until the broker holds a request, or give up after five seconds."""
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            _, payload = self._call("GET", sid, "permissions")
            if payload["pending"]:
                return payload["pending"]
            time.sleep(0.05)
        self.fail("no pending review request")

    def _wait_idle(self, sid: str) -> None:
        """Poll the session list until its turn has ended."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            _, payload = _request("GET", self._url("/v1/sessions"))
            rows = [s for s in json.loads(payload)["sessions"] if s["session_id"] == sid]
            if rows and not rows[0].get("turn_active"):
                return
            time.sleep(0.05)
        self.fail("turn never ended")

    def test_review_parks_write_and_deny_skips_it(self) -> None:
        sid = self._session()
        self.assertEqual(self._call("POST", sid, "review", {"enabled": True})[0], 200)
        status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/turn"),
                             body={"message": "write reviewed.txt secret"})
        self.assertEqual(status, 202)
        req = self._pending(sid)[0]
        self.assertEqual(req["kind"], "review")
        self.assertIn("+secret", req["args_preview"]["diff"])
        self.assertFalse((self.work / "reviewed.txt").exists())
        status, _ = self._call("POST", sid, f"permissions/{req['req_id']}", {"decision": "deny"})
        self.assertEqual(status, 200)
        self._wait_idle(sid)
        self.assertFalse((self.work / "reviewed.txt").exists())
        log = (SESSIONS_DIR / f"{sid}.jsonl").read_text(encoding="utf-8")
        self.assertIn("denied by operator", log)
        _, diff = self._call("GET", sid, "diff?turn=1")
        self.assertEqual(diff["files"], [])

    def _listed(self, sid: str) -> dict:
        """This session's row in GET /v1/sessions."""
        _, payload = _request("GET", self._url("/v1/sessions"))
        return next(s for s in json.loads(payload)["sessions"] if s["session_id"] == sid)

    def test_revert_refuses_unexplained_paths(self) -> None:
        sid = self._session()
        self._turn(sid, "write tracked.txt v")
        env = self.work / ".env"
        env.write_text("TOKEN=1\n")
        (self.work / ".gitignore").write_text(".env\n")
        try:
            for path in (".env", "no/such.txt", ".GIT/HEAD"):
                status, _ = self._call("POST", sid, "revert-file", {"path": path, "turn": 1})
                self.assertEqual(status, 404, path)
            self.assertEqual(env.read_text(), "TOKEN=1\n")
            log = (SESSIONS_DIR / f"{sid}.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("Operator reverted", log)
        finally:
            env.unlink()
            (self.work / ".gitignore").unlink()

    def test_review_allow_always_ends_review(self) -> None:
        sid = self._session()
        _, before = self._call("GET", sid, "envelope")
        self._call("POST", sid, "review", {"enabled": True})
        self.assertTrue(self._listed(sid)["review_writes"])
        _request("POST", self._url(f"/v1/sessions/{sid}/turn"), body={"message": "write all.txt yes"})
        req = self._pending(sid)[0]
        self._call("POST", sid, f"permissions/{req['req_id']}", {"decision": "allow_always"})
        self._wait_idle(sid)
        self.assertEqual((self.work / "all.txt").read_text(), "yes\n")
        self.assertFalse(self._listed(sid)["review_writes"])
        _, env = self._call("GET", sid, "envelope")
        self.assertEqual(env["envelope"]["grants"], before["envelope"]["grants"])
        self._turn(sid, "write all2.txt more")
        self.assertTrue((self.work / "all2.txt").exists())


if __name__ == "__main__":
    unittest.main()
