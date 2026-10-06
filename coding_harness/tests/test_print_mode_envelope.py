"""Print mode attaches the cwd envelope preset; HARNESS_ENVELOPE=off restores None.

Drives ``print_mode.run`` end to end with the model call mocked and reads the
``session_start`` event off the stderr JSONL stream.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core import session as session_module
from coding_harness.modes import print_mode
from coding_harness.security import audit


def _fake_chat(*, model, messages, tools, on_delta=None, **_kw):  # type: ignore[no-untyped-def]
    return {
        "role": "assistant",
        "content": "done",
        "tool_calls": [],
        "_usage": {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None},
    }


class TestPrintModeEnvelope(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(session_module, "SESSIONS_DIR", tmp_path / "sessions"),
            mock.patch("coding_harness.core.session.ollama.chat", side_effect=_fake_chat),
            mock.patch.dict(os.environ, {"HARNESS_REPO_MAP": "0", "HARNESS_TOOL_PROBE": "0"}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _session_start(self, env: dict[str, str]) -> dict:
        err = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            code = print_mode.run("hello", enable_mcp=False, force_local=True)
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in err.getvalue().splitlines() if line.startswith("{")]
        return next(e for e in events if e["event"] == "session_start")

    def test_session_start_carries_preset_envelope(self) -> None:
        start = self._session_start({"HARNESS_ENVELOPE": ""})
        grants = start["envelope"]["grants"]
        self.assertEqual(
            {g["tool"] for g in grants},
            {"Read", "Grep", "Glob", "Write", "Edit", "Bash"},
        )
        root = os.path.realpath(os.getcwd())
        read = next(g for g in grants if g["tool"] == "Read")
        self.assertEqual(read["path_glob"], f"{root}/**")
        bash = next(g for g in grants if g["tool"] == "Bash")
        self.assertEqual(bash["access"], "execute")
        self.assertIsNone(bash["expires_at"])
        self.assertEqual(bash["granted_by"], "default")

    def test_off_restores_ungated_session(self) -> None:
        start = self._session_start({"HARNESS_ENVELOPE": "off"})
        self.assertIsNone(start["envelope"])


if __name__ == "__main__":
    unittest.main()
