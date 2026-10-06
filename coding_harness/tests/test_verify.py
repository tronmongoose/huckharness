"""In-loop verify-repair.

Two layers:
  1. Unit tests for verify.verify_files — the deterministic gate.
  2. Session integration — the gate runs after an Edit/Write (or a Bash
     command that writes a file via redirect/heredoc), feeds a failure
     back as the next observation, caps at MAX_REPAIR_ROUNDS, and honors the
     kill switch.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.core import verify
from coding_harness.core.session import MAX_REPAIR_ROUNDS, Session
from coding_harness.security import audit
from coding_harness.tools.registry import DispatchEvent, ToolRegistry

# ── Unit: verify.verify_files ────────────────────────────────


class VerifyFilesTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, body: str) -> str:
        p = self.dir / name
        p.write_text(body)
        return str(p)

    def test_clean_file_passes(self) -> None:
        f = self._write("ok.py", "x = 1\n")
        self.assertIsNone(verify.verify_files([f]))

    def test_syntax_error_reported(self) -> None:
        f = self._write("bad.py", "def broken(:\n")
        report = verify.verify_files([f])
        self.assertIsNotNone(report)
        self.assertIn("py_compile", report)

    @unittest.skipUnless(verify._resolve_ruff(), "ruff")
    def test_fixable_lint_is_autofixed_not_reported(self) -> None:
        # Unused import: an F401 ruff can fix on its own.
        f = self._write("lint.py", "import os\nx = 1\n")
        self.assertIsNone(verify.verify_files([f]))
        self.assertEqual(Path(f).read_text(), "x = 1\n")

    @unittest.skipUnless(verify._resolve_ruff(), "ruff")
    def test_ruff_autofix_reports_only_the_residual(self) -> None:
        f = self._write("order.py", "import sys\nimport os\ny = undefined\nprint(sys, os)\n")
        report = verify.verify_files([f])
        self.assertTrue(Path(f).read_text().startswith("import os\nimport sys\n"))
        self.assertIsNotNone(report)
        self.assertIn("minimal Edit at that line; do not rewrite the file", report)
        # isort's blank line after the import block moves the residual to line 4.
        self.assertIn(f"{f}:4: F821 Undefined name `undefined`", report)
        self.assertNotIn("I001", report)

    @unittest.skipUnless(verify._resolve_ruff(), "ruff")
    def test_autofix_names_the_rewritten_file_in_notes(self) -> None:
        f = self._write("lint.py", "import os\nx = 1\n")
        clean = self._write("clean.py", "x = 1\n")
        notes: list[str] = []
        self.assertIsNone(verify.verify_files([f, clean], notes=notes))
        self.assertEqual(notes, [f"ruff reformatted {f}: re-read before your next Edit"])
        notes = []
        self.assertIsNone(verify.verify_files([clean], notes=notes))
        self.assertEqual(notes, [])

    def test_non_python_skipped(self) -> None:
        f = self._write("notes.md", "# not python (:\n")
        self.assertIsNone(verify.verify_files([f]))

    def test_non_python_failure_reported(self) -> None:
        f = self._write("cfg.json", '{"a": \n')
        report = verify.verify_files([f])
        self.assertIsNotNone(report)
        self.assertIn(f"json {f}:", report)
        self.assertIn("JSONDecodeError", report)

    def test_skips_are_logged_not_reported(self) -> None:
        f = self._write("x.toml", "a = 1\n")
        with patch("coding_harness.core.verifiers.sys.version_info", (3, 9, 0)), \
                self.assertLogs("coding_harness.core.verify", level="INFO") as logs:
            self.assertIsNone(verify.verify_files([f]))
        self.assertIn("needs python 3.11", "\n".join(logs.output))

    def test_missing_file_skipped(self) -> None:
        self.assertIsNone(verify.verify_files([str(self.dir / "gone.py")]))


# ── Session integration ──────────────────────────────────────


def _terminal(text: str = "done") -> dict:
    return {"role": "assistant", "content": text, "tool_calls": []}


def _edit_call(file_path: str, *, call_id: str = "c1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "function": {
                "name": "Edit",
                "arguments": json.dumps(
                    {"file_path": file_path, "old_string": "a", "new_string": "b"}
                ),
            },
        }],
    }


def _bash_call(command: str, *, call_id: str = "b1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "function": {"name": "Bash", "arguments": json.dumps({"command": command})},
        }],
    }


class _Scripted:
    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = 0

    def __call__(self, *, model, messages, tools, **_kwargs):
        # **_kwargs absorbs on_delta, passed whenever an event sink is wired.
        msg = self.messages[self.calls]
        self.calls += 1
        return msg


def _ok_result(file_path: str):
    return type(
        "R", (), {"content": "edited", "is_error": False, "metadata": {"file": file_path}}
    )()


class VerifyRepairSessionTests(unittest.TestCase):

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patches = [
            patch.object(audit, "META_DIR", self.tmp_path),
            patch.object(audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"),
            patch.object(audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"),
            patch(
                "coding_harness.core.session.SESSIONS_DIR",
                self.tmp_path / "sessions",
            ),
        ]
        for p in self._patches:
            p.start()
        self.events: list[DispatchEvent] = []

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _bad_py(self) -> str:
        p = self.tmp_path / "bad.py"
        p.write_text("def broken(:\n")
        return str(p)

    def _session(self, **kwargs) -> Session:
        kwargs.setdefault("model", "mistral-small3.2:latest")
        kwargs.setdefault("agentic_review", False)
        return Session(
            registry=ToolRegistry(),
            system_prompt="terse",
            event_sink=self.events.append,
            **kwargs,
        )

    def _verify_events(self) -> list[DispatchEvent]:
        return [e for e in self.events if e.kind == "verify_repair"]

    def _feedback_msgs(self, session) -> list[str]:
        # The report is appended to the edit's tool result, not a user message.
        return [
            m["content"] for m in session._messages
            if m["role"] == "tool" and "verify gate" in m.get("content", "")
        ]

    def test_failed_edit_feeds_report_back(self) -> None:
        bad = self._bad_py()
        scripted = _Scripted([_edit_call(bad), _terminal()])
        session = self._session()
        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch", return_value=_ok_result(bad),
        ):
            session.run_turn("fix it")
        self.assertEqual(len(self._verify_events()), 1)
        self.assertEqual(len(self._feedback_msgs(session)), 1)

    def test_repair_capped_at_max_rounds(self) -> None:
        bad = self._bad_py()
        # Model keeps emitting failing edits; the gate must stop after the cap.
        scripted = _Scripted([_edit_call(bad) for _ in range(MAX_REPAIR_ROUNDS + 3)]
                             + [_terminal()])
        session = self._session()
        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch", return_value=_ok_result(bad),
        ):
            session.run_turn("fix it")
        self.assertEqual(len(self._verify_events()), MAX_REPAIR_ROUNDS)

    def test_clean_edit_no_feedback(self) -> None:
        good = self.tmp_path / "good.py"
        good.write_text("x = 1\n")
        scripted = _Scripted([_edit_call(str(good)), _terminal()])
        session = self._session()
        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch", return_value=_ok_result(str(good)),
        ):
            session.run_turn("edit it")
        self.assertEqual(self._verify_events(), [])

    @unittest.skipUnless(verify._resolve_ruff(), "ruff")
    def test_reformat_note_rides_the_edit_result_without_a_repair_round(self) -> None:
        lint = self.tmp_path / "lint.py"
        lint.write_text("import os\nx = 1\n")
        scripted = _Scripted([_edit_call(str(lint)), _terminal()])
        session = self._session()
        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch", return_value=_ok_result(str(lint)),
        ):
            session.run_turn("edit it")
        self.assertEqual(self._verify_events(), [])
        self.assertEqual(lint.read_text(), "x = 1\n")
        tool_msgs = [m for m in session._messages if m["role"] == "tool"]
        self.assertEqual(
            tool_msgs[0]["content"],
            f"edited\n\nruff reformatted {lint}: re-read before your next Edit",
        )

    def test_kill_switch_disables_gate(self) -> None:
        bad = self._bad_py()
        scripted = _Scripted([_edit_call(bad), _terminal()])
        session = self._session(verify_repair=False)
        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch", return_value=_ok_result(bad),
        ):
            session.run_turn("fix it")
        self.assertEqual(self._verify_events(), [])


    def _bash_ok(self):
        return type(
            "R", (), {"content": "exit: 0", "is_error": False, "metadata": {"exit_code": 0}}
        )()

    def _run_bash(self, command: str, **session_kwargs) -> Session:
        # dispatch is mocked, so the file the command "wrote" is pre-seeded on
        # disk by the caller; the gate must find it from the command text alone.
        scripted = _Scripted([_bash_call(command), _terminal()])
        session = self._session(**session_kwargs)
        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch", return_value=self._bash_ok(),
        ):
            session.run_turn("do it")
        return session

    def test_bash_heredoc_write_hits_gate(self) -> None:
        bad = self._bad_py()
        session = self._run_bash(f"cat > {bad} << 'EOF'\ndef broken(:\nEOF")
        self.assertEqual(len(self._verify_events()), 1)
        self.assertEqual(
            self._verify_events()[0].payload["files"], [str(Path(bad).resolve())],
        )
        self.assertEqual(len(self._feedback_msgs(session)), 1)

    def test_bash_sed_inplace_hits_gate(self) -> None:
        bad = self._bad_py()
        self._run_bash(f"sed -i '' 's/x/y/' {bad}")
        self.assertEqual(len(self._verify_events()), 1)

    def test_bash_without_write_skips_gate(self) -> None:
        bad = self._bad_py()
        self._run_bash(f"cat {bad}")
        self.assertEqual(self._verify_events(), [])

    def test_bash_clean_write_no_feedback(self) -> None:
        good = self.tmp_path / "good.py"
        good.write_text("x = 1\n")
        session = self._run_bash(f"printf 'x = 1\\n' > {good}")
        self.assertEqual(self._verify_events(), [])
        self.assertEqual(self._feedback_msgs(session), [])

    def test_bash_write_respects_kill_switch(self) -> None:
        bad = self._bad_py()
        self._run_bash(f"cat > {bad} << 'EOF'\nx\nEOF", verify_repair=False)
        self.assertEqual(self._verify_events(), [])


if __name__ == "__main__":
    unittest.main()
