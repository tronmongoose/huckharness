"""Turn-loop event payloads and edit tracking.

A review-gate deny must not count as an edit, and per-step events carry the
prompt number as ``turn`` and the model-call count within it as ``step``.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.session import Session
from coding_harness.modes.print_mode import SYSTEM_PROMPT, build_registry
from coding_harness.security import audit
from coding_harness.tests.test_deadline import _final, _Scripted, _write_call


class TestTurnEvents(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp_path = Path(self.tmp.name)
        self.target = str(tmp_path / "out.py")
        for p in (
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", tmp_path / "sessions"),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(allowed=True, reason="test", path="hook"),
            ),
        ):
            p.start()
            self.addCleanup(p.stop)
        self.events: list = []

    def _session(self, chat, approve: bool) -> Session:
        """A session on a scripted model whose review gate approves or denies."""
        registry = build_registry(event_sink=None, enable_mcp=False)
        registry.confirm_callback = lambda _plan: approve
        session = Session(
            model="mistral-small3.2:latest", registry=registry,
            system_prompt=SYSTEM_PROMPT, event_sink=self.events.append,
            force_local=True, verify_repair=False, agentic_review=False,
        )
        patch = mock.patch("coding_harness.core.session.ollama.chat", side_effect=chat)
        patch.start()
        self.addCleanup(patch.stop)
        return session

    def _payloads(self, kind: str) -> list:
        """Payloads of every emitted event of one kind."""
        return [e.payload for e in self.events if e.kind == kind]

    def test_denied_write_is_not_a_changed_file(self) -> None:
        session = self._session(_Scripted([_write_call(self.target), _final()]), approve=False)
        result = session.run_turn("write it")
        self.assertFalse(Path(self.target).exists())
        self.assertEqual(result.files_changed, [])
        self.assertEqual(self._payloads("turn_done")[-1]["files_changed"], [])

    def test_model_call_start_sends_prompt_number_and_step(self) -> None:
        scripted = _Scripted([_write_call(self.target), _final(), _final()])
        session = self._session(scripted, approve=True)
        session.run_turn("write it")
        session.run_turn("again")
        starts = [(p["turn"], p["step"]) for p in self._payloads("model_call_start")]
        self.assertEqual(starts, [(1, 1), (1, 2), (2, 1)])

    def test_steer_injected_sends_prompt_number(self) -> None:
        scripted = _Scripted([_final(), _write_call(self.target), _final()])
        holder: list = []

        def chat(**kw):
            if scripted.calls == 1:
                holder[0].steer("use the small fix")
            return scripted(**kw)

        session = self._session(chat, approve=True)
        holder.append(session)
        session.run_turn("first")
        session.run_turn("second")
        injected = self._payloads("steer_injected")
        self.assertEqual([(p["turn"], p["step"]) for p in injected], [(2, 2)])


if __name__ == "__main__":
    unittest.main()
