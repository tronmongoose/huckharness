"""A configured brain that cannot start costs each entry point its brain, never its start."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.settings import Settings
from coding_harness.modes import print_mode
from coding_harness.security import audit
from coding_harness.tests.test_serve_brain import _serve
from coding_harness.tests.test_serve_gui import _read_events
from coding_harness.tests.test_serve_mode import _fake_ollama_chat, _request

# Fails at construction whether or not slos_recall is installed.
BROKEN = {"backend": "mcp", "agent_id": "bjorn-harness"}


def test_print_mode_registry_starts_without_the_brain(capsys) -> None:
    registry = print_mode.build_registry(enable_mcp=False, brain_block=BROKEN)
    assert "Brain" not in registry.tools and "command" in (registry.brain_error or "")
    err = capsys.readouterr().err
    assert err.count("second brain unavailable") == 1


def test_repl_registry_with_its_own_sink_warns_once(capsys) -> None:
    events: list = []
    registry = print_mode.build_registry(event_sink=events.append, enable_mcp=False,
                                         brain_block=BROKEN, subagents=True)
    assert "Brain" not in registry.tools and "Read" in registry.tools
    assert capsys.readouterr().err.count("second brain unavailable") == 1


def test_inprocess_without_the_package_starts_without_the_brain(monkeypatch, capsys) -> None:
    from coding_harness.context import brain_inprocess

    monkeypatch.setattr(brain_inprocess.importlib.util, "find_spec", lambda name: None)
    registry = print_mode.build_registry(enable_mcp=False,
                                         brain_block={"backend": "inprocess", "agent_id": "a"})
    assert "Brain" not in registry.tools and "not importable" in (registry.brain_error or "")


class TestServeWithABrokenBrain(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp),
            mock.patch("coding_harness.core.session.ollama.chat", side_effect=_fake_ollama_chat),
        ]
        for p in cls.patches:
            p.start()
        cls.server, cls.thread = _serve(Settings(brain=dict(BROKEN)), brain=True)

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

    def test_session_starts_and_says_why_the_brain_is_off(self) -> None:
        status, body = _request("POST", self._url("/v1/sessions"), body={})
        self.assertEqual(status, 201, body)
        sid = json.loads(body)["session_id"]
        events = _read_events(self._url(f"/v1/sessions/{sid}/events"), until="brain_unavailable")
        self.assertIn("command", events[-1]["params"]["reason"])
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/pin"),
                                body={"path": "a.md", "pinned": True})
        self.assertEqual(status, 502)
        self.assertIn("command", body)

    def test_status_and_search_report_the_reason(self) -> None:
        _, body = _request("GET", self._url("/v1/brain/status"))
        status = json.loads(body)
        self.assertEqual((status["configured"], status["ok"]), (True, False))
        self.assertIn("command", status["error"])
        code, _ = _request("GET", self._url("/v1/brain/search?q=x"))
        self.assertEqual(code, 502)
