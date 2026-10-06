"""Agentic review inside the session loop: the reviewer sees one unified diff
per edited file (not the whole file), a Bash-written file rides as an
unsnapshotted block, the last verify report is passed along, the event carries
backend and model, and a REVISE verdict buys exactly one repair round."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core import review
from coding_harness.core.review import ReviewResult
from coding_harness.core.session import MAX_REVIEW_ROUNDS, Session
from coding_harness.modes.print_mode import build_registry
from coding_harness.security import audit


def _terminal(text="done"):
    return {"role": "assistant", "content": text, "tool_calls": []}


def _call(name: str, args: dict, cid: str = "c1"):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "function": {"name": name, "arguments": json.dumps(args)}}]}


def _edit_call(fp: str, old: str, new: str):
    return _call("Edit", {"file_path": fp, "old_string": old, "new_string": new})


def _approve(**kw) -> ReviewResult:
    return ReviewResult(True, "", "local", "granite4.1:8b", **kw)


def _fifty_lines(changed: int | None = None) -> str:
    lines = [f"value_{i:02d} = {i}" for i in range(1, 51)]
    if changed is not None:
        lines[changed - 1] = f"value_{changed:02d} = {changed * 10}"
    return "\n".join(lines) + "\n"


class _Scripted:
    def __init__(self, msgs):
        self.msgs, self.i = list(msgs), 0

    def __call__(self, *, model, messages, tools, **_kw):
        m = self.msgs[self.i]
        self.i += 1
        return m


class ReviewSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tp = Path(self.tmp.name)
        self._p = [
            mock.patch.object(audit, "META_DIR", self.tp),
            mock.patch.object(audit, "AUDIT_PATH", self.tp / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", self.tp / "anchors.jsonl"),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", self.tp / "s"),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(allowed=True, reason="test", path="hook"),
            ),
        ]
        for p in self._p:
            p.start()
        self.events = []

    def tearDown(self):
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def _session(self, scripted: _Scripted, **kw) -> Session:
        registry = build_registry(event_sink=None, enable_mcp=False)
        registry.confirm_callback = lambda _plan: True
        kw.setdefault("verify_repair", False)
        kw.setdefault("agentic_review", True)
        session = Session(
            model="mistral-small3.2:latest", registry=registry, system_prompt="terse",
            event_sink=self.events.append, force_local=True, **kw,
        )
        patch = mock.patch("coding_harness.core.session.ollama.chat", side_effect=scripted)
        patch.start()
        self.addCleanup(patch.stop)
        return session

    def _reviews(self):
        return [e for e in self.events if e.kind == "agentic_review"]

    def test_edit_review_sees_diff_not_whole_file(self):
        f = self.tp / "code.py"
        f.write_text(_fifty_lines())
        reviewer = mock.MagicMock(return_value=_approve())
        session = self._session(_Scripted([
            _edit_call(str(f), "value_25 = 25", "value_25 = 250"), _terminal(),
        ]))
        with mock.patch.object(review, "review_change", reviewer):
            result = session.run_turn("bump 25")
        self.assertEqual(result.halted_reason, "model_done")
        task, diffs, report = reviewer.call_args.args
        self.assertEqual((task, report), ("bump 25", None))
        self.assertEqual(reviewer.call_args.kwargs,
                         {"fallback_model": "mistral-small3.2:latest", "sensitive": False})
        diff = diffs[str(f)]
        self.assertIn("@@", diff)
        self.assertIn("-value_25 = 25\n+value_25 = 250", diff)
        self.assertNotIn("value_01 = 1", diff)
        self.assertNotIn("value_50 = 50", diff)
        self.assertEqual(session._first_pre_image[str(f)], _fifty_lines().encode("utf-8"))

    def test_first_pre_image_wins_across_two_edits(self):
        f = self.tp / "code.py"
        f.write_text("a = 1\n")
        reviewer = mock.MagicMock(return_value=_approve())
        session = self._session(_Scripted([
            _edit_call(str(f), "a = 1", "a = 2"), _edit_call(str(f), "a = 2", "a = 3", ), _terminal(),
        ]))
        with mock.patch.object(review, "review_change", reviewer):
            session.run_turn("edit twice")
        diff = reviewer.call_args.args[1][str(f)]
        self.assertIn("-a = 1\n+a = 3", diff)
        self.assertNotIn("a = 2", diff)

    def test_new_file_has_none_pre_image(self):
        f = self.tp / "new.py"
        reviewer = mock.MagicMock(return_value=_approve())
        session = self._session(_Scripted([
            _call("Write", {"file_path": str(f), "content": "x = 1\n"}), _terminal(),
        ]))
        with mock.patch.object(review, "review_change", reviewer):
            session.run_turn("create it")
        self.assertIsNone(session._first_pre_image[str(f)])
        self.assertIn("@@ -0,0 +1 @@\n+x = 1", reviewer.call_args.args[1][str(f)])

    def test_bash_written_file_is_unsnapshotted_block(self):
        f = self.tp / "shell.py"
        reviewer = mock.MagicMock(return_value=_approve())
        session = self._session(_Scripted([
            _call("Bash", {"command": f"printf 'x = 1\\n' > {f}"}), _terminal(),
        ]))
        with mock.patch.object(review, "review_change", reviewer):
            session.run_turn("write via shell")
        key = str(f.resolve())
        block = reviewer.call_args.args[1][key]
        self.assertTrue(block.startswith(f"--- {key} (unsnapshotted, written by Bash) ---\n"))
        self.assertIn("x = 1", block)
        self.assertNotIn("@@", block)

    def test_review_receives_last_verify_report(self):
        f = self.tp / "code.py"
        f.write_text("a = 1\n")
        reviewer = mock.MagicMock(return_value=_approve())
        session = self._session(
            _Scripted([_edit_call(str(f), "a = 1", "a = 2"), _terminal()]), verify_repair=True,
        )
        with mock.patch.object(review, "review_change", reviewer), \
                mock.patch("coding_harness.core.session.verify.verify_files",
                           return_value="ruff: E999 boom"):
            session.run_turn("edit it")
        self.assertEqual(reviewer.call_args.args[2], "ruff: E999 boom")

    def test_agentic_review_event_carries_backend_and_model(self):
        f = self.tp / "code.py"
        f.write_text("a = 1\n")
        verdict = _approve(parse_error=True, fallback_used=True)
        session = self._session(_Scripted([_edit_call(str(f), "a = 1", "a = 2"), _terminal()]))
        with mock.patch.object(review, "review_change", return_value=verdict):
            session.run_turn("edit it")
        payload = self._reviews()[0].payload
        self.assertEqual(payload["backend"], "local")
        self.assertEqual(payload["model"], "granite4.1:8b")
        self.assertTrue(payload["parse_error"] and payload["fallback_used"] and payload["approved"])
        records = [json.loads(ln) for ln in session.session_log_path.read_text().splitlines()]
        logged = [r for r in records if r["kind"] == "agentic_review"]
        self.assertEqual(len(logged), 1)
        self.assertEqual(logged[0]["model"], "granite4.1:8b")

    def test_revise_buys_exactly_one_repair_round(self):
        f = self.tp / "code.py"
        f.write_text("a = 1\n")
        revise = ReviewResult(False, "fix the thing", "local", "granite4.1:8b")
        scripted = _Scripted([_edit_call(str(f), "a = 1", "a = 2"), _terminal(), _terminal()])
        session = self._session(scripted)
        with mock.patch.object(review, "review_change", return_value=revise):
            result = session.run_turn("edit it")
        self.assertEqual(MAX_REVIEW_ROUNDS, 1)
        self.assertEqual(len(self._reviews()), 1)
        self.assertEqual(result.halted_reason, "model_done")
        self.assertEqual(scripted.i, 3)
        self.assertIn("fix the thing", session.messages[-2]["content"])

    def test_review_off_no_review(self):
        f = self.tp / "code.py"
        f.write_text("a = 1\n")
        session = self._session(
            _Scripted([_edit_call(str(f), "a = 1", "a = 2"), _terminal()]), agentic_review=False,
        )
        with mock.patch.object(review, "review_change", side_effect=AssertionError("no review")):
            session.run_turn("edit it")
        self.assertEqual(self._reviews(), [])


if __name__ == "__main__":
    unittest.main()
