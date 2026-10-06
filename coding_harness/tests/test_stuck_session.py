"""Session wiring for the stuck detector, truncated replies and malformed calls (P1-3)."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core import recovery, stuck
from coding_harness.core.session import Session
from coding_harness.modes import print_mode
from coding_harness.modes._pretty import PrettySink
from coding_harness.modes.print_mode import SYSTEM_PROMPT, build_registry
from coding_harness.security import audit

_USAGE = {"tokens_in": 1, "tokens_out": 1, "thinking_tokens": None}


def _call(name: str, arguments: str, call_id: str = "c1") -> dict:
    return {
        "role": "assistant", "content": "",
        "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments},
        }],
    }


def _grep_call(path: str) -> dict:
    return _call("Grep", json.dumps({"pattern": "needle", "path": path}))


def _write_call(path: str) -> dict:
    return _call("Write", json.dumps({"file_path": path, "content": "x = 1\n"}))


def _edit_call(path: str) -> dict:
    return _call("Edit", json.dumps({"file_path": path, "old_string": "a", "new_string": "b"}))


def _bash_call(command: str) -> dict:
    return _call("Bash", json.dumps({"command": command}))


def _final(text: str = "done") -> dict:
    return {"role": "assistant", "content": text, "tool_calls": []}


class _Scripted:
    """Fake ollama.chat: scripted replies, records sampling kwargs per call."""

    def __init__(self, replies: list, *, finish: dict[int, str] | None = None) -> None:
        self.replies = replies
        self.finish = finish or {}
        self.kwargs: list[dict] = []
        self.calls = 0

    def __call__(self, *, model, messages, tools, on_delta=None, **kw):
        idx = self.calls
        self.calls += 1
        self.kwargs.append(kw)
        usage = dict(_USAGE, finish_reason=self.finish.get(idx, "stop"))
        return {**self.replies[idx], "_usage": usage}


class TestStuckSession(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.repo = str(tmp_path / "repo")
        Path(self.repo).mkdir()
        self.target = str(tmp_path / "repo" / "out.py")
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

    def _session(self, scripted, **kw) -> Session:
        registry = build_registry(event_sink=None, enable_mcp=False)
        registry.confirm_callback = lambda _plan: True
        session = Session(
            model="mistral-small3.2:latest", registry=registry,
            system_prompt=SYSTEM_PROMPT, event_sink=self.events.append,
            force_local=True, verify_repair=False, agentic_review=False, **kw,
        )
        patch = mock.patch("coding_harness.core.session.ollama.chat", side_effect=scripted)
        patch.start()
        self.addCleanup(patch.stop)
        return session

    def _audit_rows(self, kind: str) -> list[dict]:
        rows = [json.loads(ln) for ln in audit.AUDIT_PATH.read_text().splitlines() if ln]
        return [r for r in rows if r.get("kind", "tool_call") == kind]

    def _tool_messages(self, session: Session) -> list[dict]:
        return [m for m in session.messages if m.get("role") == "tool"]

    def _transcript(self, session: Session) -> list[dict]:
        path = session._session_log_path
        return [json.loads(ln) for ln in path.read_text().splitlines() if ln]

    def _kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def _users(self, session: Session) -> list[str]:
        return [m["content"] for m in session.messages if m.get("role") == "user"]

    def _render(self) -> str:
        """Replay the captured events through the REPL renderer; must not raise."""
        buf = io.StringIO()
        sink = PrettySink(stream=buf)
        for event in self.events:
            sink(event)
        return buf.getvalue()

    def test_four_identical_greps_halt_as_stuck(self) -> None:
        scripted = _Scripted([_grep_call(self.repo)] * 6 + [_final()])
        session = self._session(scripted)
        result = session.run_turn("find it")
        self.assertEqual(result.halted_reason, "stuck")
        self.assertEqual(result.final_text, stuck.HALT_TEXT)
        self.assertEqual(result.turns, 4)
        self.assertEqual(scripted.calls, 4)
        self.assertIsNone(result.error)
        self.assertEqual(
            [r["halted_reason"] for r in self._audit_rows("turn")], ["stuck"],
        )
        tool_msgs = self._tool_messages(session)
        self.assertEqual(len(tool_msgs), 4)
        self.assertNotIn(stuck.nudge_text, tool_msgs[0]["content"])
        self.assertIn(stuck.nudge_text, tool_msgs[1]["content"])
        kinds = self._kinds()
        self.assertIn("stuck_nudge", kinds)
        self.assertEqual(kinds.count("stuck"), 1)
        self.assertEqual(self.events[-1].kind, "turn_done")
        self.assertEqual(self.events[-1].payload["halted_reason"], "stuck")
        # The nudge rides the tool result, so rewind's user-turn count holds.
        self.assertEqual(self._users(session), ["find it"])
        self.assertIn("Grep", self._render())

    def test_repeated_failing_edit_halts_as_stuck(self) -> None:
        missing = str(Path(self.repo) / "missing.py")
        scripted = _Scripted([_edit_call(missing)] * 5 + [_final()])
        session = self._session(scripted)
        result = session.run_turn("edit it")
        self.assertEqual(result.halted_reason, "stuck")
        self.assertEqual(scripted.calls, 4)
        self.assertFalse(Path(missing).exists())
        tool_msgs = self._tool_messages(session)
        self.assertEqual(len(tool_msgs), 4)
        self.assertIn(stuck.nudge_text, tool_msgs[1]["content"])

    def test_repeated_successful_bash_is_not_stuck(self) -> None:
        # A successful Bash mutates, so five identical runs never count as a
        # repeat; without the reset the fourth would halt the prompt.
        scripted = _Scripted([_bash_call("true")] * 5 + [_final()])
        result = self._session(scripted).run_turn("run it")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(scripted.calls, 6)
        self.assertNotIn("stuck_nudge", self._kinds())

    def test_repeated_failing_bash_halts_as_stuck(self) -> None:
        scripted = _Scripted([_bash_call("false")] * 5 + [_final()])
        result = self._session(scripted).run_turn("run it")
        self.assertEqual(result.halted_reason, "stuck")
        self.assertEqual(scripted.calls, 4)

    def test_print_mode_exits_one_on_stuck(self) -> None:
        scripted = _Scripted([_grep_call(self.repo)] * 6 + [_final()])
        out, err = io.StringIO(), io.StringIO()
        env = {"HARNESS_REPO_MAP": "0", "HARNESS_TOOL_PROBE": "0"}
        with mock.patch("coding_harness.core.session.ollama.chat", side_effect=scripted), \
                mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = print_mode.run("find it", enable_mcp=False, force_local=True)
        self.assertEqual(code, 1)
        self.assertEqual(out.getvalue().strip(), stuck.HALT_TEXT)
        lines = [json.loads(ln) for ln in err.getvalue().splitlines() if ln.startswith("{")]
        summary = next(e for e in lines if e["event"] == "summary")
        self.assertEqual(summary["halted_reason"], "stuck")
        self.assertEqual(summary["turns"], 4)

    def test_successful_write_between_greps_resets(self) -> None:
        scripted = _Scripted([
            _grep_call(self.repo), _write_call(self.target), _grep_call(self.repo), _final(),
        ])
        result = self._session(scripted).run_turn("do it")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertNotIn("stuck_nudge", self._kinds())
        self.assertNotIn("stuck", self._kinds())

    def test_kill_switch_disables_detection(self) -> None:
        scripted = _Scripted([_grep_call(self.repo)] * 5 + [_final()])
        with mock.patch.dict(os.environ, {"HARNESS_STUCK_DETECT": "0"}):
            result = self._session(scripted).run_turn("find it")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(scripted.calls, 6)
        self.assertNotIn("stuck_nudge", self._kinds())

    def test_truncated_write_is_not_dispatched(self) -> None:
        scripted = _Scripted([_write_call(self.target), _final()], finish={0: "length"})
        session = self._session(scripted)
        result = session.run_turn("write it")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertFalse(Path(self.target).exists())
        self.assertEqual(result.files_changed, [])
        # The call stays on the assistant turn and is answered by a tool
        # error, so the transcript replays and rewind counts one user turn.
        assistant = [m for m in session.messages if m.get("role") == "assistant"]
        self.assertEqual(len(assistant[0]["tool_calls"]), 1)
        tool_msgs = self._tool_messages(session)
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertEqual(tool_msgs[0]["content"], recovery.TRUNCATED_TEXT)
        self.assertEqual(self._users(session), ["write it"])
        self.assertEqual(self._kinds().count("truncated_response"), 1)
        logged = [r for r in self._transcript(session) if r["kind"] == "tool_result"]
        self.assertEqual(len(logged), 1)
        self.assertTrue(logged[0]["is_error"])
        self.assertEqual(logged[0]["error_class"], "truncated")
        self.assertIn("truncated", self._render())
        profile_predict = session.profile.num_predict
        self.assertEqual(scripted.kwargs[0]["max_tokens"], profile_predict)
        self.assertEqual(scripted.kwargs[1]["max_tokens"], min(8192, profile_predict * 2))

    def test_truncation_budget_caps_at_8192(self) -> None:
        scripted = _Scripted(
            [_write_call(self.target)] * 3 + [_final()],
            finish={0: "length", 1: "length", 2: "length"},
        )
        result = self._session(scripted).run_turn("write it")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(scripted.kwargs[3]["max_tokens"], 8192)

    def test_length_without_tool_calls_is_a_normal_reply(self) -> None:
        scripted = _Scripted([_final("cut off text")], finish={0: "length"})
        result = self._session(scripted).run_turn("say it")
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(result.final_text, "cut off text")
        self.assertNotIn("truncated_response", self._kinds())

    def test_malformed_arguments_are_rejected_without_dispatch(self) -> None:
        scripted = _Scripted([_call("Write", '{"file_path": '), _final()])
        session = self._session(scripted)
        with mock.patch.object(session.registry, "dispatch") as dispatch:
            result = session.run_turn("write it")
        dispatch.assert_not_called()
        self.assertEqual(result.halted_reason, "model_done")
        tool_msgs = self._tool_messages(session)
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertIn("arguments were not valid JSON", tool_msgs[0]["content"])
        self.assertIn("resend the call with valid JSON", tool_msgs[0]["content"])
        rows = self._audit_rows("tool_call")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tool"], "Write")
        self.assertFalse(rows[0]["allowed"])
        self.assertEqual(rows[0]["error"], "invalid_args")
        self.assertEqual(rows[0]["sentinel_reason"], "invalid_args")
        self.assertEqual(rows[0]["sentinel_path"], "registry")
        invalid = [e for e in self.events if e.kind == "tool_args_invalid"]
        self.assertEqual(len(invalid), 1)
        self.assertEqual(invalid[0].payload["tool"], "Write")
        self.assertIn("blocked: invalid_args", self._render())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
