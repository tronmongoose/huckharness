"""Paste-echo gate.

Two layers of coverage:
  1. Heuristic unit tests for ``is_paste_echo`` — pure, no I/O.
  2. Session integration tests — confirms the gate sits between the model
     emitting a Bash tool_call and ``registry.dispatch``, and that the
     deny path lands a ``paste_echo_denied`` audit entry while the model
     receives a synthetic refusal.

The repro fixture is the literal 2026-05-07 case from production: a REPL
prompt of ``python3 -m coding_harness --force-local "anything goes"`` that
the model echoed back as a Bash command. If this test ever flakes, the
gate has weakened — fix the heuristic, not the test.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.core.session import Session
from coding_harness.security import audit
from coding_harness.security.paste_echo import is_paste_echo
from coding_harness.tools.registry import ToolRegistry

# ── Heuristic unit tests ─────────────────────────────────────


class IsPasteEchoTests(unittest.TestCase):

    def test_2026_05_07_repro_harness_self_invocation(self) -> None:
        # Verbatim repro from the live production session.
        prompt = 'python3 -m coding_harness --force-local "anything goes"'
        command = 'python3 -m coding_harness --force-local "anything goes"'
        matches, reason = is_paste_echo(prompt, command)
        self.assertTrue(matches)
        # Harness pattern hits first; even a small quote tweak still fires it.
        self.assertEqual(reason, "harness_self_invocation")

    def test_harness_invocation_with_python_no_3(self) -> None:
        matches, reason = is_paste_echo(
            "do something", "python -m coding_harness --help"
        )
        self.assertTrue(matches)
        self.assertEqual(reason, "harness_self_invocation")

    def test_near_verbatim_echo_with_quote_tweak(self) -> None:
        prompt = "echo hello world from the prompt"
        # Same words, model added a stray quote — still ≥0.80 similarity.
        command = "echo 'hello world from the prompt'"
        matches, reason = is_paste_echo(prompt, command)
        self.assertTrue(matches)
        self.assertTrue(reason.startswith("verbatim_echo"))

    def test_long_substring_containment(self) -> None:
        prompt = "summarize the changes in core/router.py"
        command = (
            "bash -c 'echo summarize the changes in core/router.py | wc -l'"
        )
        matches, reason = is_paste_echo(prompt, command)
        self.assertTrue(matches)
        self.assertEqual(reason, "prompt_substring_in_command")

    # ── Negatives — no regression on legitimate Bash dispatch ──

    def test_legitimate_ls_does_not_match(self) -> None:
        matches, reason = is_paste_echo(
            "list the files in coding_harness/core", "ls coding_harness/core"
        )
        self.assertFalse(matches, msg=f"unexpected match: {reason}")

    def test_legitimate_git_status_does_not_match(self) -> None:
        matches, reason = is_paste_echo("show git status", "git status")
        self.assertFalse(matches, msg=f"unexpected match: {reason}")

    def test_short_prompt_short_circuits(self) -> None:
        # "ls" as prompt is too short to drive substring matching.
        self.assertEqual(is_paste_echo("ls", "ls -la"), (False, ""))

    def test_empty_inputs_do_not_crash(self) -> None:
        self.assertEqual(is_paste_echo("", "ls"), (False, ""))
        self.assertEqual(is_paste_echo("anything", ""), (False, ""))


# ── Session integration tests ────────────────────────────────


def _terminal(text: str = "ok") -> dict:
    return {"role": "assistant", "content": text, "tool_calls": []}


def _bash_tool_call(command: str, *, call_id: str = "c1") -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "function": {
                "name": "Bash",
                "arguments": json.dumps({"command": command}),
            },
        }],
    }


class _Scripted:
    """Fake ollama.chat that yields a scripted sequence of assistant messages."""
    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = 0

    def __call__(self, *, model, messages, tools, **_kw):
        msg = self.messages[self.calls]
        self.calls += 1
        return msg


class PasteEchoSessionTests(unittest.TestCase):
    """The gate fires before registry.dispatch and audits the denial."""

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

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _build_session(self, **kwargs) -> Session:
        kwargs.setdefault("model", "mistral-small3.2:latest")
        return Session(
            registry=ToolRegistry(),
            system_prompt="terse",
            **kwargs,
        )

    def _read_audit(self) -> list[dict]:
        path = self.tmp_path / "audit.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_paste_echo_denied_skips_dispatch_and_audits(self) -> None:
        prompt = 'python3 -m coding_harness --force-local "anything goes"'
        # Round 1: model emits the paste-echo Bash call.
        # Round 2: model wraps up after seeing the synthetic denial.
        scripted = _Scripted([
            _bash_tool_call(prompt),
            _terminal("understood, won't run that"),
        ])
        session = self._build_session(paste_echo_callback=lambda *a, **kw: False)

        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch",
        ) as dispatch_mock:
            session.run_turn(prompt)

        dispatch_mock.assert_not_called()

        entries = self._read_audit()
        denied = [e for e in entries if e.get("sentinel_path") == "paste_echo"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["sentinel_reason"], "paste_echo_denied")
        self.assertFalse(denied[0]["allowed"])
        self.assertTrue(denied[0]["denied_by_operator"])

    def test_paste_echo_allowed_proceeds_to_dispatch(self) -> None:
        prompt = 'python3 -m coding_harness --force-local "anything goes"'
        scripted = _Scripted([
            _bash_tool_call(prompt),
            _terminal("done"),
        ])
        session = self._build_session(paste_echo_callback=lambda *a, **kw: True)

        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch",
            return_value=type("R", (), {"content": "ran", "is_error": False, "metadata": {}})(),
        ) as dispatch_mock:
            session.run_turn(prompt)

        dispatch_mock.assert_called_once()
        # No paste_echo audit entry on the allow path — Sentinel/registry handle audit.
        denied = [
            e for e in self._read_audit()
            if e.get("sentinel_path") == "paste_echo"
        ]
        self.assertEqual(denied, [])

    def test_default_callback_none_denies(self) -> None:
        # paste_echo_callback unset → safe default is deny (matches print mode).
        prompt = 'python3 -m coding_harness --force-local "x"'
        scripted = _Scripted([
            _bash_tool_call(prompt),
            _terminal("ack"),
        ])
        session = self._build_session()  # no callback

        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(session.registry, "dispatch") as dispatch_mock:
            session.run_turn(prompt)

        dispatch_mock.assert_not_called()

    def test_non_bash_tool_calls_are_not_gated(self) -> None:
        # Even if Read args echo the prompt, only Bash gets gated. Read goes
        # through registry.dispatch normally.
        prompt = 'show me /tmp/x'
        read_call = {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"file_path": prompt}),
                },
            }],
        }
        scripted = _Scripted([read_call, _terminal("done")])
        # paste_echo_callback would deny everything if it ever got called —
        # this test proves it never gets called for non-Bash tools.
        session = self._build_session(
            paste_echo_callback=lambda *a, **kw: False,
        )

        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch",
            return_value=type("R", (), {"content": "ok", "is_error": False, "metadata": {}})(),
        ) as dispatch_mock:
            session.run_turn(prompt)

        dispatch_mock.assert_called_once()

    def test_legitimate_bash_passes_through(self) -> None:
        # The classic non-regression: prompt "list files in core" + Bash
        # "ls coding_harness/core" must NOT trip the gate.
        prompt = "list the files in coding_harness/core"
        scripted = _Scripted([
            _bash_tool_call("ls coding_harness/core"),
            _terminal("here are the files"),
        ])
        session = self._build_session(
            paste_echo_callback=lambda *a, **kw: False,  # would deny if gated
        )

        with patch(
            "coding_harness.core.session.ollama.chat", side_effect=scripted,
        ), patch.object(
            session.registry, "dispatch",
            return_value=type("R", (), {"content": "files...", "is_error": False, "metadata": {}})(),
        ) as dispatch_mock:
            session.run_turn(prompt)

        dispatch_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
