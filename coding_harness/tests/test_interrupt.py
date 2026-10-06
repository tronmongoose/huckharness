"""Tests for cooperative turn interruption (P1).

``Session.interrupt()`` sets an Event; the turn thread notices at its next
checkpoint (top of the agent loop, between streamed deltas, between tool
calls) and unwinds to ``halted_reason="interrupted"``. A blocking model read
can't be killed mid-request, so cancellation is cooperative — these tests
drive the checkpoints deterministically rather than racing a wall clock.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.session import Session
from coding_harness.modes.print_mode import SYSTEM_PROMPT, build_registry
from coding_harness.security import audit


class TestInterrupt(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
        ]
        for p in self.patches:
            p.start()
        # Sessions write their JSONL transcript under a real dir; redirect it.
        self.sessions_patch = mock.patch(
            "coding_harness.core.session.SESSIONS_DIR", tmp_path / "sessions"
        )
        self.sessions_patch.start()

    def tearDown(self) -> None:
        self.sessions_patch.stop()
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _session(self, chat_side_effect, event_sink=lambda _e: None) -> Session:
        registry = build_registry(event_sink=None, enable_mcp=False)
        registry.confirm_callback = lambda _plan: True
        session = Session(
            model="mistral-small3.2:latest",
            registry=registry,
            system_prompt=SYSTEM_PROMPT,
            event_sink=event_sink,
            force_local=True,
        )
        self._chat_patch = mock.patch(
            "coding_harness.core.session.ollama.chat",
            side_effect=chat_side_effect,
        )
        self._chat_patch.start()
        self.addCleanup(self._chat_patch.stop)
        return session

    def test_interrupt_mid_stream_halts_turn(self) -> None:
        holder: dict[str, Session] = {}

        def _chat(*, model, messages, tools, on_delta=None, **_kw):
            # Emit one token, trip the interrupt, then the next delta raises.
            if on_delta is not None:
                on_delta("first ")
                holder["session"].interrupt()
                on_delta("second ")  # raises _Interrupted at the checkpoint
            return {
                "role": "assistant", "content": "unreached",
                "tool_calls": [],
                "_usage": {"tokens_in": 1, "tokens_out": 1, "thinking_tokens": None},
            }

        session = self._session(_chat)
        holder["session"] = session
        result = session.run_turn("do a thing")
        self.assertEqual(result.halted_reason, "interrupted")

    def test_interrupt_before_turn_short_circuits(self) -> None:
        # An interrupt set before the loop's first checkpoint stops the turn
        # before any model call. (run_turn clears the flag at entry, so we set
        # it from within the first on_delta-free chat via a pre-flagged event.)
        calls = {"n": 0}

        def _chat(*, model, messages, tools, on_delta=None, **_kw):
            calls["n"] += 1
            return {
                "role": "assistant", "content": "done",
                "tool_calls": [],
                "_usage": {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None},
            }

        session = self._session(_chat, event_sink=None)
        # With no event_sink, on_delta is never wired, so the turn runs to
        # model_done normally — establishes the non-interrupted baseline.
        result = session.run_turn("hello")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(calls["n"], 1)

    def test_interrupt_is_cleared_between_turns(self) -> None:
        holder: dict[str, Session] = {}
        state = {"interrupt_once": True}

        def _chat(*, model, messages, tools, on_delta=None, **_kw):
            if on_delta is not None and state["interrupt_once"]:
                on_delta("x ")
                holder["session"].interrupt()
                on_delta("y ")
            return {
                "role": "assistant", "content": "ok",
                "tool_calls": [],
                "_usage": {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None},
            }

        session = self._session(_chat)
        holder["session"] = session
        first = session.run_turn("turn one")
        self.assertEqual(first.halted_reason, "interrupted")
        # Second turn must not inherit the interrupt flag.
        state["interrupt_once"] = False
        second = session.run_turn("turn two")
        self.assertEqual(second.halted_reason, "model_done")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
