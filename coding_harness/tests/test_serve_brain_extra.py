"""Pin, memory-proposal and skill-suggest routes on a real server over the fake index."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.context.skills import Skill
from coding_harness.core import memory_store, paths
from coding_harness.core.settings import Settings
from coding_harness.modes import serve_brain_extra
from coding_harness.security import audit
from coding_harness.tests import fake_brain
from coding_harness.tests.test_memory_proposals import _cand
from coding_harness.tests.test_serve_brain import _serve
from coding_harness.tests.test_serve_gui import _read_events
from coding_harness.tests.test_serve_mode import _fake_ollama_chat, _request


class TestServeBrainExtra(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        skills = [Skill("code-review", "Review a diff for correctness bugs", tmp / "SKILL.md")]
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp),
            mock.patch("coding_harness.core.session.ollama.chat", side_effect=_fake_ollama_chat),
            mock.patch.object(serve_brain_extra, "index_skills", return_value=skills),
            mock.patch.dict(os.environ, {"HARNESS_MEMORY_DIR": str(paths.meta_dir() / "serve-mem")}),
            mock.patch.object(memory_store.importlib, "import_module", side_effect=ImportError),
        ]
        for p in cls.patches:
            p.start()
        cls.server, cls.thread = _serve(Settings(brain=fake_brain.block(tmp)), brain=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{path}"

    def _session(self) -> str:
        _, body = _request("POST", self._url("/v1/sessions"), body={})
        return json.loads(body)["session_id"]

    def test_skill_suggest(self) -> None:
        _, body = _request("GET", self._url("/v1/skills/suggest?q=review%20my%20diff"))
        rows = json.loads(body)["suggestions"]
        self.assertEqual([r["name"] for r in rows], ["code-review"])
        _, body = _request("GET", self._url("/v1/skills/suggest?q="))
        self.assertEqual(json.loads(body), {"suggestions": []})

    def test_pin_and_unpin(self) -> None:
        sid = self._session()
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/pin"),
                                body={"path": "finance/budget.md", "pinned": True})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["tier"], 2)
        _, listing = _request("GET", self._url(f"/v1/sessions/{sid}/memories"))
        self.assertEqual(json.loads(listing)["pinned"], [{"path": "finance/budget.md", "tier": 2}])
        status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/pin"),
                             body={"path": "finance/budget.md", "pinned": False})
        self.assertEqual(status, 200)
        _, listing = _request("GET", self._url(f"/v1/sessions/{sid}/memories"))
        self.assertEqual(json.loads(listing)["pinned"], [])
        events = _read_events(self._url(f"/v1/sessions/{sid}/events"), until="note_pinned")
        self.assertEqual(events[-1]["params"], {"path": "finance/budget.md", "pinned": True, "tier": 2})
        status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/pin"), body={"path": "nope.md"})
        self.assertEqual(status, 502)
        status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/pin"),
                             body={"path": "x.md", "pinned": "yes"})
        self.assertEqual(status, 400)

    def test_list_and_decide_proposals(self) -> None:
        sid = self._session()
        memory_store.write_proposal(sid, _cand(name="serve-approved"), 1)
        memory_store.write_proposal(sid, _cand(name="serve-rejected"), 1)
        _, listing = _request("GET", self._url(f"/v1/sessions/{sid}/memories"))
        rows = json.loads(listing)["proposals"]
        self.assertEqual([r["name"] for r in rows], ["serve-approved", "serve-rejected"])
        approve, reject = rows
        url = self._url(f"/v1/sessions/{sid}/memories")
        status, _ = _request("POST", url, body={
            "id": approve["id"], "decision": "approve", "sha256": "0" * 64})
        self.assertEqual(status, 409)
        status, body = _request("POST", url, body={
            "id": approve["id"], "decision": "approve", "sha256": approve["sha256"]})
        self.assertEqual(status, 200, body)
        self.assertTrue(Path(json.loads(body)["path"]).is_file())
        events = _read_events(self._url(f"/v1/sessions/{sid}/events"), until="memory_decided")
        self.assertEqual(events[-1]["params"]["decision"], "approve")
        decision = {"id": reject["id"], "decision": "reject", "sha256": reject["sha256"]}
        status, _ = _request("POST", url, body=decision)
        self.assertEqual(status, 200)
        status, _ = _request("POST", url, body=decision)
        self.assertEqual(status, 404)
        _, listing = _request("GET", self._url(f"/v1/sessions/{sid}/memories"))
        self.assertEqual(json.loads(listing)["proposals"], [])

    def test_openapi_lists_the_routes(self) -> None:
        _, body = _request("GET", self._url("/v1/openapi.json"))
        paths_ = json.loads(body)["paths"]
        for route in ("/v1/skills/suggest", "/v1/sessions/{id}/memories", "/v1/sessions/{id}/pin"):
            self.assertIn(route, paths_)


def test_suggest_rereads_skills_at_most_every_ttl(monkeypatch) -> None:
    monkeypatch.setattr(serve_brain_extra, "_SKILLS_CACHE", {})
    index = mock.Mock(return_value=[])
    monkeypatch.setattr(serve_brain_extra, "index_skills", index)
    clock = iter([100.0, 102.0, 106.0])
    monkeypatch.setattr(serve_brain_extra.time, "monotonic", lambda: next(clock))
    for _ in range(3):
        serve_brain_extra._skills("/repo")
    assert index.call_count == 2
