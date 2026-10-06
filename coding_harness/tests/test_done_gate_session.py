"""Green-before-done gate in the session loop (P1-4), with the scripted fake
chat: done -> checks fail -> repair message -> done -> pass, capped at two
rounds; a red baseline skips; the kill switches; targeted tests appended to
the edit's tool result; and the CLI --check / print-mode plumbing."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness import cli
from coding_harness.core import done_gate, review
from coding_harness.core.done_gate import PYTEST
from coding_harness.core.review import ReviewResult
from coding_harness.core.session import MAX_TURNS, Session, SessionResult
from coding_harness.modes import print_mode
from coding_harness.modes.print_mode import build_registry
from coding_harness.security import audit

TEST_MOD = """\
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mod import x

def test_x():
    assert x == {expected}
"""


def _terminal(text: str = "done") -> dict:
    return {"role": "assistant", "content": text, "tool_calls": []}


def _edit_call(fp: str, old: str, new: str, cid: str = "c1") -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "function": {"name": "Edit", "arguments": json.dumps(
            {"file_path": fp, "old_string": old, "new_string": new})}}]}


def _bash_call(command: str, cid: str = "b1") -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "function": {"name": "Bash", "arguments": json.dumps({"command": command})}}]}


class _Scripted:
    def __init__(self, msgs):
        self.msgs, self.i = list(msgs), 0

    def __call__(self, *, model, messages, tools, **_kw):
        m = self.msgs[self.i]
        self.i += 1
        return m


class _SessionBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._old_cwd)
        self._p = [
            mock.patch.object(audit, "META_DIR", self.root),
            mock.patch.object(audit, "AUDIT_PATH", self.root / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", self.root / "anchors.jsonl"),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", self.root / "s"),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(allowed=True, reason="test", path="hook"),
            ),
        ]
        for p in self._p:
            p.start()
        self.events: list = []
        self.mod = self._write("mod.py", "x = 1\n")
        self._write("check.sh", 'grep -q "x = 1" mod.py\n')

    def tearDown(self) -> None:
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def _write(self, name: str, body: str = "") -> str:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        return str(p)

    def _session(self, scripted: _Scripted, **kw) -> Session:
        registry = build_registry(event_sink=None, enable_mcp=False)
        registry.confirm_callback = lambda _plan: True
        kw.setdefault("verify_repair", False)
        kw.setdefault("agentic_review", False)
        kw.setdefault("done_gate_enabled", True)
        kw.setdefault("targeted_tests_enabled", False)
        kw.setdefault("done_checks", ["bash check.sh"])
        kw = {k: v for k, v in kw.items() if v is not ...}  # ... means "use the env default"
        session = Session(
            model="mistral-small3.2:latest", registry=registry, system_prompt="terse",
            event_sink=self.events.append, force_local=True, **kw,
        )
        patch = mock.patch("coding_harness.core.session.ollama.chat", side_effect=scripted)
        patch.start()
        self.addCleanup(patch.stop)
        return session

    def _events(self, kind: str) -> list:
        return [e.payload for e in self.events if e.kind == kind]

    def _repair_msgs(self, session: Session) -> list[str]:
        return [m["content"] for m in session.messages
                if m["role"] == "user" and m["content"].startswith("The checks below failed")]

    def _break(self, cid: str = "c1") -> dict:
        return _edit_call(self.mod, "x = 1", "x = 2", cid)

    def _fix(self, cid: str = "c2") -> dict:
        return _edit_call(self.mod, "x = 2", "x = 1", cid)


class GateSessionTests(_SessionBase):
    def test_fail_then_repair_then_pass(self) -> None:
        scripted = _Scripted([self._break(), _terminal(), self._fix(), _terminal()])
        session = self._session(scripted)
        result = session.run_turn("edit it")
        self.assertEqual(self._events("checks_baseline"), [{"failures": 0, "checks": ["bash check.sh"]}])
        gates = self._events("done_gate")
        self.assertEqual([(g["round"], g["passed"]) for g in gates], [(1, False), (2, True)])
        self.assertEqual(gates[0]["failures"], ["bash check.sh"])
        repairs = self._repair_msgs(session)
        self.assertEqual(len(repairs), 1)
        self.assertIn("FAIL: bash check.sh", repairs[0])
        self.assertEqual((result.halted_reason, result.checks_passed, scripted.i), ("model_done", True, 4))
        self.assertIn("PASS: bash check.sh", result.checks_report)

    def test_capped_at_two_repair_rounds(self) -> None:
        scripted = _Scripted([self._break(), _terminal(), _terminal(), _terminal(), _terminal()])
        session = self._session(scripted)
        result = session.run_turn("edit it")
        self.assertEqual(done_gate.MAX_GATE_ROUNDS, 2)
        self.assertEqual([g["passed"] for g in self._events("done_gate")], [False, False, False])
        self.assertEqual(len(self._repair_msgs(session)), 2)
        self.assertEqual((result.halted_reason, result.checks_passed, scripted.i), ("model_done", False, 4))
        self.assertIn("FAIL: bash check.sh", result.checks_report)

    def test_red_baseline_skips_the_gate(self) -> None:
        self._write("check.sh", 'grep -q "x = 2" mod.py\n')
        other = self._write("other.py", "a = 1\n")
        scripted = _Scripted([_edit_call(other, "a = 1", "a = 2"), _terminal(), _terminal()])
        session = self._session(scripted)
        result = session.run_turn("edit other")
        self.assertEqual(self._events("checks_baseline")[0]["failures"], 1)
        gate = self._events("done_gate")[0]
        self.assertEqual((gate["passed"], gate["skipped_reason"]), (None, "baseline_red:1 failures"))
        self.assertEqual(self._repair_msgs(session), [])
        self.assertEqual((result.checks_passed, scripted.i), (None, 2))

    def test_red_baseline_turned_green_passes(self) -> None:
        self._write("check.sh", 'grep -q "x = 2" mod.py\n')
        result = self._session(_Scripted([self._break(), _terminal()])).run_turn("fix it")
        self.assertIs(result.checks_passed, True)
        self.assertEqual([g["passed"] for g in self._events("done_gate")], [True])

    def test_kill_switch_disables_gate_and_baseline(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_DONE_GATE": "0"}):
            session = self._session(_Scripted([self._break(), _terminal()]), done_gate_enabled=...)
        result = session.run_turn("edit it")
        self.assertFalse(session.done_gate_enabled)
        self.assertEqual(self._events("checks_baseline") + self._events("done_gate"), [])
        self.assertEqual((result.checks_passed, result.checks_report), (None, ""))
        with mock.patch.dict(os.environ, {"HARNESS_DONE_GATE": "1"}):
            self.assertTrue(self._session(_Scripted([]), done_gate_enabled=...).done_gate_enabled)

    def test_deadline_reserve_skips_gate(self) -> None:
        result = self._session(_Scripted([self._break(), _terminal()])).run_turn("edit", deadline_s=30)
        self.assertEqual([s["reason"] for s in self._events("checks_skipped")], ["deadline_reserve"])
        self.assertEqual(self._events("done_gate"), [])
        self.assertEqual((result.halted_reason, result.checks_passed), ("model_done", None))

    def test_bash_step_counts_as_an_edit_for_the_gate(self) -> None:
        result = self._session(_Scripted([_bash_call("echo hi > out.txt"), _terminal()])).run_turn("run it")
        self.assertEqual(self._events("checks_skipped"), [])
        self.assertEqual([g["passed"] for g in self._events("done_gate")], [True])
        self.assertIs(result.checks_passed, True)

    def test_gate_runs_before_review_and_again_after_its_repair(self) -> None:
        reviewer = mock.MagicMock(return_value=ReviewResult(False, "rename it", "local", "m"))
        scripted = _Scripted([_edit_call(self.mod, "x = 1", "x = 1\ny = 2"), _terminal(),
                              self._break(), _terminal(), self._fix(), _terminal()])
        with mock.patch.object(review, "review_change", reviewer):
            result = self._session(scripted, agentic_review=True).run_turn("edit it")
        kinds = [e.kind for e in self.events if e.kind in ("done_gate", "agentic_review")]
        self.assertEqual(kinds, ["done_gate", "agentic_review", "done_gate", "done_gate"])
        self.assertEqual([g["passed"] for g in self._events("done_gate")], [True, False, True])
        self.assertEqual((result.checks_passed, reviewer.call_count, scripted.i), (True, 1, 6))

    def test_edits_after_the_gate_make_the_verdict_unknown(self) -> None:
        # The gate passes, the reviewer asks for changes, and the model keeps
        # editing until MAX_TURNS: the green verdict describes a tree that no
        # longer exists, so it must not be reported as True.
        reviewer = mock.MagicMock(return_value=ReviewResult(False, "rename it", "local", "m"))
        edits = [self._break(f"b{i}") if i % 2 == 0 else self._fix(f"f{i}") for i in range(MAX_TURNS)]
        scripted = _Scripted([_edit_call(self.mod, "x = 1", "x = 1\ny = 2"), _terminal(), *edits])
        with mock.patch.object(review, "review_change", reviewer):
            result = self._session(scripted, agentic_review=True).run_turn("edit it")
        self.assertEqual(result.halted_reason, "max_turns")
        self.assertEqual([g["passed"] for g in self._events("done_gate")], [True])
        self.assertIsNone(result.checks_passed)
        self.assertTrue(result.checks_report.startswith("PASS: bash check.sh"))
        self.assertTrue(result.checks_report.endswith(f"\n{MAX_TURNS - 2} edits after the last gate run"))

    def test_no_edits_skips_baseline_and_gate(self) -> None:
        result = self._session(_Scripted([_terminal()])).run_turn("just answer")
        self.assertEqual(self._events("checks_baseline_start") + self._events("checks_baseline"), [])
        self.assertEqual([s["reason"] for s in self._events("checks_skipped")], ["no_edits"])
        self.assertIsNone(result.checks_passed)

    def test_read_only_bash_takes_no_baseline(self) -> None:
        result = self._session(_Scripted([_bash_call("ls"), _terminal()])).run_turn("look")
        self.assertEqual(self._events("checks_baseline"), [])
        self.assertEqual([s["reason"] for s in self._events("checks_skipped")], ["no_edits"])
        self.assertIsNone(result.checks_passed)

    def test_baseline_is_announced_before_the_first_edit_runs(self) -> None:
        edit = _edit_call(self.mod, "x = 1", "x = 1\ny = 2")
        self._session(_Scripted([_bash_call("ls"), edit, _terminal()])).run_turn("edit it")
        kinds = [e.kind for e in self.events
                 if e.kind in ("checks_baseline_start", "checks_baseline", "tool_call_start")]
        # ls ran before any baseline; the baseline lands between ls and the edit.
        self.assertEqual(kinds, ["tool_call_start", "checks_baseline_start",
                                 "checks_baseline", "tool_call_start"])

    def test_discovery_runs_pytest_when_no_checks(self) -> None:
        self._write("tests/test_mod.py", TEST_MOD.format(expected=10))
        # The fix changes the file size: baseline and edit land in the same
        # second, and a same-size rewrite would keep the stale mod.pyc.
        fix = _edit_call(self.mod, "x = 1", "x = 10")
        result = self._session(_Scripted([fix, _terminal()]), done_checks=[]).run_turn("edit")
        self.assertEqual(self._events("checks_baseline")[0]["checks"], [shlex.join(PYTEST)])
        self.assertIs(result.checks_passed, True)

    def test_pinned_test_cmd(self) -> None:
        edit = _edit_call(self.mod, "x = 1", "x = 1\ny = 2")
        self._session(_Scripted([edit, _terminal()]), done_checks=[],
                      test_cmd=["bash", "check.sh"]).run_turn("hi")
        self.assertEqual(self._events("checks_baseline")[0]["checks"], ["bash check.sh"])


class TargetedSessionTests(_SessionBase):
    def setUp(self) -> None:
        super().setUp()
        self._write("tests/test_mod.py", TEST_MOD.format(expected=1))

    def _tool_msgs(self, session: Session) -> list[str]:
        return [m["content"] for m in session.messages if m["role"] == "tool"]

    def test_failure_appended_to_the_edit_result(self) -> None:
        session = self._session(_Scripted([self._break(), _terminal()]),
                                done_gate_enabled=False, targeted_tests_enabled=True)
        session.run_turn("edit it")
        targeted = self._events("targeted_tests")
        self.assertEqual([(t["round"], t["files"]) for t in targeted], [(1, [self.mod])])
        content = self._tool_msgs(session)[0]
        self.assertIn("Tests covering your edit failed", content)
        self.assertIn("test_mod.py::test_x", content)

    def test_green_tests_add_nothing(self) -> None:
        session = self._session(_Scripted([_edit_call(self.mod, "x = 1", "x = 1\ny = 2"), _terminal()]),
                                done_gate_enabled=False, targeted_tests_enabled=True)
        session.run_turn("edit it")
        self.assertEqual(self._events("targeted_tests"), [])
        self.assertNotIn("Tests covering", self._tool_msgs(session)[0])

    def test_skipped_when_the_edit_failed_verify(self) -> None:
        session = self._session(_Scripted([_edit_call(self.mod, "x = 1", "x = ("), _terminal()]),
                                done_gate_enabled=False, targeted_tests_enabled=True, verify_repair=True)
        session.run_turn("edit it")
        self.assertEqual(len(self._events("verify_repair")), 1)
        self.assertEqual(self._events("targeted_tests"), [])

    def test_kill_switch(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_TARGETED_TESTS": "0"}):
            session = self._session(_Scripted([self._break(), _terminal()]),
                                    done_gate_enabled=False, targeted_tests_enabled=...)
        session.run_turn("edit it")
        self.assertFalse(session.targeted_tests_enabled)
        self.assertEqual(self._events("targeted_tests"), [])


class CliCheckTests(unittest.TestCase):
    def test_check_flag_threads_into_print_mode(self) -> None:
        with mock.patch("coding_harness.cli.print_mode.run", return_value=0) as run:
            self.assertEqual(cli.main(["--check", "make test", "--check", "pytest -q", "--no-mcp", "hi"]), 0)
        self.assertEqual(run.call_args.kwargs["checks"], ["make test", "pytest -q"])
        with mock.patch("coding_harness.cli.print_mode.run", return_value=0) as run:
            cli.main(["--no-mcp", "hi"])
        self.assertEqual(run.call_args.kwargs["checks"], [])

    def test_print_mode_passes_done_checks_and_summarizes(self) -> None:
        result = SessionResult(
            final_text="ok", turns=1, session_id="s", session_log_path=Path("/tmp/x.jsonl"),
            halted_reason="model_done", checks_passed=True, checks_report="PASS: make test",
        )
        fake = mock.MagicMock()
        fake.run.return_value = result
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("coding_harness.modes.print_mode.Session", return_value=fake) as cls, \
                mock.patch("coding_harness.modes.print_mode.build_system_prompt", return_value="p"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = print_mode.run("hi", enable_mcp=False, force_local=True, checks=["make test"])
        self.assertEqual(code, 0)
        self.assertEqual(cls.call_args.kwargs["done_checks"], ["make test"])
        summary = json.loads(err.getvalue().strip().splitlines()[-1])
        self.assertEqual((summary["event"], summary["checks_passed"]), ("summary", True))


if __name__ == "__main__":
    unittest.main()
