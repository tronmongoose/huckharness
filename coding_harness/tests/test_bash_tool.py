"""Bash tool error classification (P0-3): unit table, end-to-end, and the
session transcript record carrying ``error_class``."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pytest

from coding_harness.core.session import Session
from coding_harness.modes.print_mode import SYSTEM_PROMPT, build_registry
from coding_harness.security import audit
from coding_harness.security.sentinel import SentinelVerdict
from coding_harness.tools.bash import MAX_OUTPUT, Bash, _truncate, classify_error
from coding_harness.tools.registry import ToolRegistry


@pytest.mark.parametrize(
    ("returncode", "stderr", "expected"),
    [
        (0, "", None),
        (0, "usage: foo", None),
        (127, "", "harness_caused:command_not_found"),
        (1, "bash: ruff: command not found", "harness_caused:command_not_found"),
        (2, "error: unexpected argument '--fix-all' found", "harness_caused:bad_flag"),
        (2, "pytest: error: unrecognized arguments: --nope", "harness_caused:bad_flag"),
        (2, "Error: No such option: --bogus", "harness_caused:bad_flag"),
        (1, "grep: invalid option -- 'Z'", "harness_caused:bad_flag"),
        (1, "unknown flag: --wat", "harness_caused:bad_flag"),
        (2, "Usage: tool [options]", "harness_caused:bad_flag"),
        (2, "make: *** No rule to make target `tset'.  Stop.", "harness_caused:missing_target"),
        (1, "FAILED tests/test_x.py::test_y - AssertionError", "informative"),
        (1, "", "informative"),
    ],
)
def test_classify_error_table(returncode, stderr, expected):
    assert classify_error(returncode, stderr) == expected


def test_nonexistent_command_end_to_end():
    result = Bash().run({"command": "definitely_not_a_command_xyz --version"})
    assert result.is_error
    assert result.metadata["exit_code"] == 127
    assert result.metadata["error_class"] == "harness_caused:command_not_found"


def test_success_has_no_error_class():
    result = Bash().run({"command": "true"})
    assert not result.is_error
    assert "error_class" not in result.metadata


def test_timeout_is_harness_caused():
    result = Bash().run({"command": "sleep 5", "timeout": 1})
    assert result.is_error
    assert result.metadata["error_class"] == "harness_caused:timeout"


def _gone(pid: int) -> bool:
    """True once ``pid`` is dead or a zombie awaiting reap; polls for up to 3 s."""
    for _ in range(30):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        stat = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        if stat.stdout.strip().startswith("Z"):
            return True
        time.sleep(0.1)
    return False


def test_timeout_kills_the_whole_process_group(tmp_path):
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    result = Bash().run({"command": f"sleep 30 & echo $! > {pidfile}; sleep 30", "timeout": 1})
    assert time.monotonic() - started < 5
    assert result.metadata["error_class"] == "harness_caused:timeout"
    pid = int(pidfile.read_text())
    assert _gone(pid), f"backgrounded grandchild {pid} outlived the call"


def test_backgrounded_pipe_holder_does_not_hang_the_call(tmp_path):
    # bash exits at once but its child keeps stdout open; the call must end at
    # the timeout, not when the grandchild lets go of the pipe.
    started = time.monotonic()
    result = Bash().run({"command": "sleep 30 & echo started", "timeout": 1})
    assert time.monotonic() - started < 5
    assert result.metadata["error_class"] == "harness_caused:timeout"


def test_timeout_is_clamped_to_the_turn_deadline():
    tool = Bash()
    tool.deadline_remaining_s = lambda: 0.5
    started = time.monotonic()
    result = tool.run({"command": "sleep 5", "timeout": 30})
    assert time.monotonic() - started < 3
    assert result.metadata["error_class"] == "harness_caused:timeout"
    assert "timed out after 1s" in result.content
    tool.deadline_remaining_s = lambda: float("inf")
    assert tool._timeout_s(30) == 30


def test_registry_hands_bash_its_deadline():
    registry = ToolRegistry()
    tool = Bash()
    registry.register(tool)
    assert tool.deadline_remaining_s == registry.deadline_remaining_s
    assert tool._timeout_s(7) == 7
    registry.deadline_at = time.time() + 0.5
    started = time.monotonic()
    result = tool.run({"command": "sleep 5"})
    assert time.monotonic() - started < 3
    assert result.metadata["error_class"] == "harness_caused:timeout"
    registry.deadline_at = None
    assert tool._timeout_s(None) == 120


def test_output_is_cut_middle_out():
    text = "".join(f"line{i:05d}\n" for i in range(2000))
    out = _truncate(text)
    assert MAX_OUTPUT == 8000
    assert len(out) <= MAX_OUTPUT
    assert out.startswith("line00000\nline00001\n")
    assert out.endswith("line01998\nline01999\n")
    marker = f"\n... [truncated {len(text) - MAX_OUTPUT} chars from the middle] ...\n"
    head, tail = out.split(marker)
    assert abs(len(head) / (len(head) + len(tail)) - 0.6) < 0.01
    assert _truncate("short") == "short"


def test_long_command_output_keeps_head_and_tail():
    result = Bash().run({"command": "seq 1 5000"})
    assert not result.is_error
    assert "\n1\n2\n" in result.content
    assert result.content.endswith("4999\n5000\n")
    assert "chars from the middle" in result.content
    assert len(result.content) < MAX_OUTPUT + 200


class TestSessionErrorClass(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", tmp_path / "s"),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=SentinelVerdict(allowed=True, reason="test", path="hook"),
            ),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_tool_result_record_and_event_carry_error_class(self) -> None:
        events: list[tuple[str, dict]] = []
        replies = iter([
            {
                "role": "assistant", "content": "", "tool_calls": [{
                    "id": "c1",
                    "function": {
                        "name": "Bash",
                        "arguments": json.dumps({"command": "definitely_not_a_command_xyz"}),
                    },
                }],
                "_usage": {"tokens_in": 1, "tokens_out": 1, "thinking_tokens": None},
            },
            {
                "role": "assistant", "content": "done", "tool_calls": [],
                "_usage": {"tokens_in": 1, "tokens_out": 1, "thinking_tokens": None},
            },
        ])

        def _chat(*, model, messages, tools, on_delta=None, **_kw):
            return next(replies)

        registry = build_registry(
            event_sink=lambda ev: events.append((ev.kind, ev.payload)),
            enable_mcp=False,
        )
        session = Session(
            model="mistral-small3.2:latest",
            registry=registry,
            system_prompt=SYSTEM_PROMPT,
            force_local=True,
        )
        with mock.patch("coding_harness.core.session.ollama.chat", side_effect=_chat):
            result = session.run_turn("go")
        self.assertEqual(result.final_text, "done")

        records = [
            json.loads(line)
            for line in session.session_log_path.read_text(encoding="utf-8").splitlines()
        ]
        tool_results = [r for r in records if r["kind"] == "tool_result"]
        self.assertEqual(len(tool_results), 1)
        self.assertTrue(tool_results[0]["is_error"])
        self.assertEqual(
            tool_results[0]["error_class"], "harness_caused:command_not_found"
        )

        payloads = [p for k, p in events if k == "tool_call_result"]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["error_class"], "harness_caused:command_not_found")


def test_secret_env_names_do_not_reach_the_child(monkeypatch):
    from coding_harness.tools import bash as bash_mod

    monkeypatch.setenv("FOO_TOKEN", "s3cret")
    monkeypatch.setenv("PLAIN_NAME", "kept")
    done = bash_mod._run_group('echo "${FOO_TOKEN:-absent} ${PLAIN_NAME:-absent}"', 10)
    assert done.stdout.strip() == "absent kept"
