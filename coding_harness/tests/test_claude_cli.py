"""Tests for the Max-plan text-only frontier backend (models/claude_cli.py).

No real subprocess: a fake Popen emits canned stream-json. The live path is
covered by the P3 serve smoke, not unit tests.
"""
from __future__ import annotations

import io
import json
import unittest
from unittest import mock

from coding_harness.models import claude_cli
from coding_harness.models.anthropic import FrontierBlockedError
from coding_harness.models.ollama import BannedModelError


def _stream_lines(*events: dict) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    }


def _thinking(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    }


def _result(text: str, tokens_in: int = 10, tokens_out: int = 5) -> dict:
    return {
        "type": "result",
        "result": text,
        "usage": {
            "input_tokens": tokens_in,
            "cache_creation_input_tokens": 100,
            "cache_read_input_tokens": 20,
            "output_tokens": tokens_out,
        },
    }


class _FakePopen:
    def __init__(self, stdout_text: str, returncode: int = 0, stderr: str = ""):
        self.stdout = io.StringIO(stdout_text)
        self.returncode = returncode
        self._stderr = stderr
        self.killed = False

    def communicate(self, timeout: float | None = None):
        return "", self._stderr

    def kill(self):
        self.killed = True


def _frontier_decision():
    d = mock.MagicMock()
    d.sensitivity_flag = False
    d.route = "frontier"
    return d


def _sensitive_decision():
    d = mock.MagicMock()
    d.sensitivity_flag = True
    d.route = "frontier"
    d.sensitivity = {"reasons": ["finance"]}
    return d


class TestClaudeCliChat(unittest.TestCase):
    def _chat(self, fake: _FakePopen, **kw):
        with mock.patch.object(
            claude_cli.subprocess, "Popen", return_value=fake
        ) as popen:
            msg = claude_cli.chat(
                model="sonnet",
                messages=[
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "hi"},
                ],
                decision=_frontier_decision(),
                **kw,
            )
        return msg, popen

    def test_assembles_content_and_usage(self) -> None:
        fake = _FakePopen(_stream_lines(
            _thinking("hm"), _delta("po"), _delta("ng"), _result("pong"),
        ))
        msg, _ = self._chat(fake)
        self.assertEqual(msg["content"], "pong")
        self.assertEqual(msg["tool_calls"], [])
        # input = 10 + 100 cache_creation + 20 cache_read
        self.assertEqual(msg["_usage"]["tokens_in"], 130)
        self.assertEqual(msg["_usage"]["tokens_out"], 5)

    def test_on_delta_gets_text_not_thinking(self) -> None:
        fake = _FakePopen(_stream_lines(
            _thinking("secret reasoning"), _delta("a"), _delta("b"), _result("ab"),
        ))
        seen: list[str] = []
        msg, _ = self._chat(fake, on_delta=seen.append)
        self.assertEqual(seen, ["a", "b"])
        self.assertEqual(msg["content"], "ab")

    def test_missing_result_falls_back_to_deltas(self) -> None:
        fake = _FakePopen(_stream_lines(_delta("par"), _delta("tial")))
        msg, _ = self._chat(fake)
        self.assertEqual(msg["content"], "partial")

    def test_nonzero_exit_raises(self) -> None:
        fake = _FakePopen("", returncode=1, stderr="boom")
        with self.assertRaises(RuntimeError):
            self._chat(fake)

    def test_no_output_at_all_raises(self) -> None:
        fake = _FakePopen("")
        with self.assertRaises(RuntimeError):
            self._chat(fake)

    def test_on_delta_exception_kills_subprocess(self) -> None:
        fake = _FakePopen(_stream_lines(_delta("x"), _result("x")))

        def _boom(_t: str) -> None:
            raise KeyboardInterrupt()

        with mock.patch.object(claude_cli.subprocess, "Popen", return_value=fake):
            with self.assertRaises(KeyboardInterrupt):
                claude_cli.chat(
                    model="sonnet",
                    messages=[{"role": "user", "content": "hi"}],
                    decision=_frontier_decision(),
                    on_delta=_boom,
                )
        self.assertTrue(fake.killed)

    def test_tools_flag_disables_all_tools(self) -> None:
        fake = _FakePopen(_stream_lines(_result("ok")))
        _, popen = self._chat(fake)
        cmd = popen.call_args[0][0]
        i = cmd.index("--tools")
        self.assertEqual(cmd[i + 1], "")
        self.assertIn("--no-session-persistence", cmd)

    def test_api_key_stripped_from_env(self) -> None:
        fake = _FakePopen(_stream_lines(_result("ok")))
        with mock.patch.dict(
            claude_cli.os.environ, {"ANTHROPIC_API_KEY": "sk-test"}
        ):
            _, popen = self._chat(fake)
        env = popen.call_args[1]["env"]
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_claude_home_becomes_the_subprocess_home(self) -> None:
        fake = _FakePopen(_stream_lines(_result("ok")))
        with mock.patch.dict(claude_cli.os.environ,
                             {"HOME": "/tmp/eval-home", "HARNESS_CLAUDE_HOME": "/Users/op"}):
            _, popen = self._chat(fake)
            self.assertEqual(claude_cli.os.environ["HOME"], "/tmp/eval-home")
        self.assertEqual(popen.call_args[1]["env"]["HOME"], "/Users/op")

    def test_home_unchanged_without_claude_home(self) -> None:
        fake = _FakePopen(_stream_lines(_result("ok")))
        with mock.patch.dict(claude_cli.os.environ, {"HOME": "/tmp/eval-home"}):
            claude_cli.os.environ.pop("HARNESS_CLAUDE_HOME", None)
            _, popen = self._chat(fake)
        self.assertEqual(popen.call_args[1]["env"]["HOME"], "/tmp/eval-home")


class TestGates(unittest.TestCase):
    def test_sensitive_decision_refuses_before_subprocess(self) -> None:
        with mock.patch.object(claude_cli.subprocess, "Popen") as popen:
            with self.assertRaises(FrontierBlockedError):
                claude_cli.chat(
                    model="sonnet",
                    messages=[{"role": "user", "content": "my balance"}],
                    decision=_sensitive_decision(),
                )
            popen.assert_not_called()

    def test_no_decision_refuses(self) -> None:
        with self.assertRaises(FrontierBlockedError):
            claude_cli.chat(
                model="sonnet",
                messages=[{"role": "user", "content": "x"}],
                decision=None,
            )

    def test_banned_model_refuses(self) -> None:
        with mock.patch.object(claude_cli.subprocess, "Popen") as popen:
            with self.assertRaises(BannedModelError):
                claude_cli.chat(
                    model="qwen-max",
                    messages=[{"role": "user", "content": "x"}],
                    decision=_frontier_decision(),
                )
            popen.assert_not_called()


class TestFlatten(unittest.TestCase):
    def test_flatten_includes_all_roles(self) -> None:
        prompt = claude_cli._flatten_messages([
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "tool", "name": "Read", "content": "file data"},
            {"role": "user", "content": "q2"},
        ])
        self.assertIn("<system>\nsys\n</system>", prompt)
        self.assertIn("User: q1", prompt)
        self.assertIn("Assistant: a1", prompt)
        self.assertIn("[Read result]\nfile data", prompt)
        self.assertIn("no tools in this", prompt)


class TestSessionDispatch(unittest.TestCase):
    """The frontier_backend='claude-cli' session routes a complexity_high turn
    to claude_cli even when generation.local_only is true (Max-plan bypass)."""

    def setUp(self) -> None:
        import tempfile
        from pathlib import Path

        from coding_harness.security import audit
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", tmp_path / "s"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_claude_cli_backend_used_for_frontier_turn(self) -> None:
        from coding_harness.core.session import Session
        from coding_harness.modes.print_mode import SYSTEM_PROMPT
        from coding_harness.tools.registry import ToolRegistry

        frontier = mock.MagicMock()
        frontier.route = "frontier"
        frontier.sensitivity_flag = False
        frontier.classification = {"route": "frontier", "score": 0.9}

        # claude-cli is text-only; a tool-bearing turn is kept local by design
        #, so the frontier path is only reachable with no tools.
        session = Session(
            model="mistral-small3.2:latest",
            registry=ToolRegistry(),
            system_prompt=SYSTEM_PROMPT,
            frontier_backend="claude-cli",
        )

        cli_reply = {
            "role": "assistant", "content": "frontier answer", "tool_calls": [],
            "_usage": {"tokens_in": 5, "tokens_out": 3, "thinking_tokens": None},
        }
        with mock.patch(
            "coding_harness.core.session.decide_route", return_value=frontier
        ), mock.patch(
            "coding_harness.core.session.generation_local_only", return_value=True
        ), mock.patch(
            "coding_harness.core.session.claude_cli.chat", return_value=cli_reply
        ) as cli_chat, mock.patch(
            "coding_harness.core.session.anthropic.chat"
        ) as api_chat, mock.patch(
            "coding_harness.core.session.ollama.chat"
        ) as ollama_chat:
            result = session.run_turn("do something hard")

        cli_chat.assert_called_once()
        api_chat.assert_not_called()
        ollama_chat.assert_not_called()
        self.assertEqual(result.final_text, "frontier answer")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
