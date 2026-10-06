"""Brain routes and note attachments on a real server over the fake index."""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.settings import Settings
from coding_harness.modes import serve_mode
from coding_harness.security import audit
from coding_harness.tests import fake_brain
from coding_harness.tests.test_serve_gui import _read_events
from coding_harness.tests.test_serve_mode import _fake_ollama_chat, _request


def _serve(settings: Settings, brain: bool):
    ready = threading.Event()
    box: dict = {}

    def _ready(server):
        box["server"] = server
        ready.set()

    thread = threading.Thread(target=serve_mode.run, daemon=True, kwargs=dict(
        host="127.0.0.1", port=0, enable_mcp=False, ready_callback=_ready,
        settings=settings, brain=brain))
    thread.start()
    assert ready.wait(5.0)
    return box["server"], thread


class TestServeBrain(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp),
            mock.patch("coding_harness.core.session.ollama.chat", side_effect=_fake_ollama_chat),
            mock.patch("coding_harness.tools.registry.sentinel.review",
                       return_value=mock.MagicMock(allowed=True, reason="t", path="hook")),
        ]
        for p in cls.patches:
            p.start()
        settings = Settings(brain=fake_brain.block(tmp))
        cls.server, cls.thread = _serve(settings, brain=True)
        cls.fleet, cls.fleet_thread = _serve(settings, brain=False)

    @classmethod
    def tearDownClass(cls) -> None:
        for server, thread in ((cls.server, cls.thread), (cls.fleet, cls.fleet_thread)):
            server.shutdown()
            server.server_close()
            thread.join(timeout=2.0)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def _url(self, path: str, server=None) -> str:
        return f"http://127.0.0.1:{(server or self.server).server_port}{path}"

    def test_status_search_and_page(self) -> None:
        _, body = _request("GET", self._url("/v1/brain/status"))
        self.assertEqual(json.loads(body), {"configured": True, "backend": "mcp", "ok": True,
                                            "age_hours": None, "stale": False, "upgrade_pending": False,
                                            "warnings": []})
        _, body = _request("GET", self._url("/v1/brain/search?q=larkspur&vault=finance"))
        hits = json.loads(body)["hits"]
        self.assertEqual([(h["path"], h["tier"]) for h in hits], [("finance/budget.md", 2)])
        _, body = _request("GET", self._url("/v1/brain/page?path=health/visit.md"))
        self.assertEqual(json.loads(body)["tier"], 3)

    def test_a_server_without_brain_never_reaches_the_index(self) -> None:
        _, body = _request("GET", self._url("/v1/brain/status", self.fleet))
        self.assertEqual(json.loads(body)["configured"], False)
        status, _ = _request("GET", self._url("/v1/brain/search?q=larkspur", self.fleet))
        self.assertEqual(status, 404)

    def test_attaching_a_confidential_note_pins_the_session_local(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/turn"), body={
            "message": "what is this budget?", "attach": ["finance/budget.md"], "wait": True})
        self.assertEqual(status, 200, body)
        events = _read_events(self._url(f"/v1/sessions/{sid}/events"), until="turn_done")
        methods = [e["method"] for e in events]
        self.assertLess(methods.index("sensitive_context"), methods.index("turn_start"))
        prompt = next(e for e in events if e["method"] == "turn_start")["params"]["prompt"]
        self.assertTrue(prompt.startswith("what is this budget?\n\n---\n\nAttached notes:"))
        self.assertIn("1200 a month", prompt)

    def test_attach_of_an_unknown_note_is_a_clear_error(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/turn"), body={
            "message": "x", "attach": ["nope.md"]})
        self.assertEqual(status, 502)
        self.assertIn("not indexed", body)
