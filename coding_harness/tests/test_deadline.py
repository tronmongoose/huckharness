"""Tests for the per-turn wall-clock deadline (P0-1).

Deadline expiry is a cooperative checkpoint, like interrupt: the fake chat
sleeps past the budget and the next ``on_delta`` unwinds the turn to
``halted_reason="deadline"``. The edited files are reported on every exit
path so a caller can keep work that already landed on disk.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from coding_harness import cli
from coding_harness.core.review import ReviewResult
from coding_harness.core.session import MAX_TURNS, Session
from coding_harness.models import transport
from coding_harness.modes.print_mode import SYSTEM_PROMPT, build_registry
from coding_harness.security import audit

_USAGE = {"tokens_in": 1, "tokens_out": 1, "thinking_tokens": None}


def _write_call(path: str) -> dict:
    return {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {
                "name": "Write",
                "arguments": json.dumps({"file_path": path, "content": "x = 1\n"}),
            },
        }],
    }


def _final(text: str = "done") -> dict:
    return {"role": "assistant", "content": text, "tool_calls": []}


class _Scripted:
    """Fake ollama.chat: scripted replies, optional sleep per step, records timeouts."""

    def __init__(self, replies: list, *, sleep_before: dict[int, float] | None = None) -> None:
        self.replies = replies
        self.sleep_before = sleep_before or {}
        self.timeouts: list = []
        self.calls = 0

    def __call__(self, *, model, messages, tools, on_delta=None, timeout=None, **_kw):
        idx = self.calls
        self.calls += 1
        self.timeouts.append(timeout)
        time.sleep(self.sleep_before.get(idx, 0))
        reply = self.replies[idx]
        if isinstance(reply, Exception):
            # Raised before any delta so the checkpoint inside on_delta cannot
            # pre-empt the path under test (the post-deadline error re-raise).
            raise reply
        if on_delta is not None:
            on_delta("tick ")
        return {**reply, "_usage": dict(_USAGE)}


class TestDeadline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.target = str(tmp_path / "out.py")
        self.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", tmp_path / "sessions"),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(allowed=True, reason="test", path="hook"),
            ),
        ]
        for p in self.patches:
            p.start()
        self.events: list = []

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _session(self, scripted: _Scripted, **kw) -> Session:
        registry = build_registry(event_sink=None, enable_mcp=False)
        registry.confirm_callback = lambda _plan: True
        kw.setdefault("agentic_review", False)
        session = Session(
            model="mistral-small3.2:latest", registry=registry,
            system_prompt=SYSTEM_PROMPT, event_sink=self.events.append,
            force_local=True, verify_repair=False, **kw,
        )
        patch = mock.patch("coding_harness.core.session.ollama.chat", side_effect=scripted)
        patch.start()
        self.addCleanup(patch.stop)
        return session

    def _audit_turn_rows(self) -> list[dict]:
        rows = [json.loads(ln) for ln in audit.AUDIT_PATH.read_text().splitlines() if ln]
        return [r for r in rows if r.get("kind") == "turn"]

    def test_deadline_halts_and_reports_files(self) -> None:
        scripted = _Scripted([_write_call(self.target), _final()], sleep_before={1: 0.35})
        session = self._session(scripted)
        result = session.run_turn("write it", deadline_s=0.2)
        self.assertEqual(result.halted_reason, "deadline")
        self.assertIsNone(result.error)
        self.assertEqual(result.files_changed, [self.target])
        self.assertTrue(Path(self.target).is_file())
        self.assertIn("deadline reached after 2 steps", result.final_text)
        self.assertIn(self.target, result.final_text)
        # Model reads are capped to the remaining budget, floored at 5 s.
        self.assertEqual(scripted.timeouts, [5.0, 5.0])
        self.assertEqual([e.kind for e in self.events if e.kind == "deadline"], ["deadline"])
        rows = self._audit_turn_rows()
        self.assertEqual([r["halted_reason"] for r in rows], ["deadline"])

    def test_deadline_mirrors_onto_registry_for_broker_waits(self) -> None:
        session = self._session(_Scripted([_final(), _final()]))
        session.run_turn("quick", deadline_s=30)
        self.assertIsNotNone(session.registry.deadline_at)
        self.assertLessEqual(session.registry.deadline_remaining_s(), 30.0)
        self.assertGreater(session.registry.deadline_remaining_s(), 20.0)
        session.run_turn("again")
        self.assertIsNone(session.registry.deadline_at)
        self.assertEqual(session.registry.deadline_remaining_s(), float("inf"))

    def test_late_transport_error_is_reported_as_deadline(self) -> None:
        # A read timeout sized to the deadline is the budget expiring, not a
        # server fault: the work on disk must not be thrown away as "error".
        scripted = _Scripted(
            [_write_call(self.target), RuntimeError("ollama transport error: timed out")],
            sleep_before={1: 0.3},
        )
        session = self._session(scripted)
        result = session.run_turn("write it", deadline_s=0.2)
        # Two calls proves the error itself, not the top-of-loop checkpoint,
        # is what unwound the turn.
        self.assertEqual(scripted.calls, 2)
        self.assertEqual(result.halted_reason, "deadline")
        self.assertIsNone(result.error)
        self.assertEqual(result.files_changed, [self.target])

    def test_review_skipped_under_deadline_reserve(self) -> None:
        reviewer = mock.MagicMock(return_value=ReviewResult(True, "", "local", "m"))
        with mock.patch("coding_harness.core.review.review_change", reviewer):
            session = self._session(
                _Scripted([_write_call(self.target), _final()]), agentic_review=True,
            )
            result = session.run_turn("write it", deadline_s=30)
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(result.files_changed, [self.target])
        reviewer.assert_not_called()
        skipped = [e for e in self.events if e.kind == "review_skipped"]
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0].payload["reason"], "deadline_reserve")

    def test_review_runs_when_reserve_allows(self) -> None:
        reviewer = mock.MagicMock(return_value=ReviewResult(True, "", "local", "m"))
        with mock.patch("coding_harness.core.review.review_change", reviewer), \
                mock.patch.dict(os.environ, {"HARNESS_DEADLINE_RESERVE_S": "0"}):
            session = self._session(
                _Scripted([_write_call(self.target), _final()]), agentic_review=True,
            )
            result = session.run_turn("write it", deadline_s=30)
        self.assertEqual(result.halted_reason, "model_done")
        reviewer.assert_called_once()
        self.assertEqual([e for e in self.events if e.kind == "review_skipped"], [])

    def test_files_changed_on_model_done(self) -> None:
        scripted = _Scripted([_write_call(self.target), _final()])
        result = self._session(scripted).run_turn("write it")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(result.files_changed, [self.target])
        # No deadline: ollama.chat is called with its default read timeout.
        self.assertEqual(scripted.timeouts, [None, None])

    def test_files_changed_on_error(self) -> None:
        scripted = _Scripted([_write_call(self.target), RuntimeError("boom")])
        result = self._session(scripted).run_turn("write it")
        self.assertEqual(result.halted_reason, "error")
        self.assertIn("boom", result.error or "")
        self.assertEqual(result.files_changed, [self.target])

    def test_dispatch_exception_is_an_error_result_with_files(self) -> None:
        scripted = _Scripted([_write_call(self.target), _write_call(self.target), _final()])
        session = self._session(scripted)
        real_dispatch = session.registry.dispatch

        def _dispatch(name, args):
            if scripted.calls == 2:
                raise ValueError("boom in dispatch")
            return real_dispatch(name, args)

        with mock.patch.object(session.registry, "dispatch", side_effect=_dispatch):
            result = session.run_turn("write it")
        self.assertEqual(result.halted_reason, "error")
        self.assertEqual(result.error, "ValueError: boom in dispatch")
        self.assertEqual(result.turns, 2)
        self.assertEqual(result.files_changed, [self.target])
        self.assertTrue(Path(self.target).is_file())
        kinds = [e.kind for e in self.events]
        self.assertIn("error", kinds)
        self.assertEqual(kinds[-1], "turn_done")
        self.assertEqual(self.events[-1].payload["halted_reason"], "error")
        self.assertEqual([r["halted_reason"] for r in self._audit_turn_rows()], ["error"])

    def test_model_calls_run_inside_the_deadline_retry_scope(self) -> None:
        scripted = _Scripted([_final(), _final()])
        seen: list = []

        def _chat(**kw):
            seen.append(getattr(transport._scope, "limits", None))
            return scripted(**kw)

        session = self._session(_chat)
        session.run_turn("quick", deadline_s=30)
        deadline, attempts = seen[0]
        self.assertIsNotNone(deadline)
        self.assertLessEqual(deadline - time.monotonic(), 30.0)
        self.assertIsNone(attempts)
        session.run_turn("again")
        self.assertEqual(seen[1], (None, None))
        self.assertIsNone(getattr(transport._scope, "limits", None))

    def test_reviewer_gets_one_attempt_under_a_deadline(self) -> None:
        seen: list = []

        def _review(*args, **kw):
            seen.append(getattr(transport._scope, "limits", None))
            return ReviewResult(True, "", "local", "m")

        with mock.patch("coding_harness.core.review.review_change", _review), \
                mock.patch.dict(os.environ, {"HARNESS_DEADLINE_RESERVE_S": "0"}):
            session = self._session(
                _Scripted([_write_call(self.target), _final(), _write_call(self.target), _final()]),
                agentic_review=True,
            )
            session.run_turn("write it", deadline_s=30)
            session.run_turn("again")
        self.assertEqual(len(seen), 2)
        self.assertIsNotNone(seen[0][0])
        self.assertEqual(seen[0][1], 1)
        self.assertEqual(seen[1], (None, None))

    def test_files_changed_on_interrupt(self) -> None:
        scripted = _Scripted([_write_call(self.target), _final()])
        holder: dict[str, Session] = {}

        def _chat(**kw):
            if scripted.calls == 1:
                holder["session"].interrupt()
            return scripted(**kw)

        session = self._session(_chat)
        holder["session"] = session
        result = session.run_turn("write it")
        self.assertEqual(result.halted_reason, "interrupted")
        self.assertEqual(result.files_changed, [self.target])

    def test_files_changed_on_max_turns(self) -> None:
        scripted = _Scripted([_write_call(self.target)] * MAX_TURNS)
        result = self._session(scripted).run_turn("write it")
        self.assertEqual(result.halted_reason, "max_turns")
        self.assertEqual(result.files_changed, [self.target])


class TestMaxTimeFlag(unittest.TestCase):
    def test_max_time_threads_into_print_mode(self) -> None:
        with mock.patch("coding_harness.cli.print_mode.run", return_value=0) as run:
            self.assertEqual(cli.main(["--max-time", "30", "--no-mcp", "hi"]), 0)
        self.assertEqual(run.call_args.kwargs["max_time_s"], 30.0)

    def test_max_time_rejects_non_positive(self) -> None:
        with self.assertRaises(SystemExit):
            cli.main(["--max-time", "0", "hi"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
