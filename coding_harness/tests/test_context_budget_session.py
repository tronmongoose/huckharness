"""Session wiring for the context budget: tool caps, Read elision, mid-turn compaction.

A fake ollama.chat enforces a hard context ceiling (num_ctx tokens) the way a
real server truncates or errors, so these tests show a 25-step turn finishing
under the budget and failing without it.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core import context_budget, replay
from coding_harness.core.review import ReviewResult
from coding_harness.core.session import MAX_TURNS, Session
from coding_harness.modes.print_mode import build_registry
from coding_harness.security import audit

NUM_CTX = 12_500
_USAGE = {"tokens_in": 1, "tokens_out": 1, "thinking_tokens": None, "finish_reason": "stop"}


def _call(cid: str, name: str, **args) -> dict:
    """An assistant reply carrying one tool call."""
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }]}


def _normalized(messages: list[dict]) -> list[dict]:
    """Messages with empty tool_calls dropped; replay omits them on a final reply."""
    return [{k: v for k, v in m.items() if not (k == "tool_calls" and not v)} for m in messages]


def _assert_well_formed(case: unittest.TestCase, messages: list[dict]) -> None:
    """Every tool result follows an assistant tool_call it can answer."""
    for i, m in enumerate(messages):
        if m.get("role") != "tool":
            continue
        j = i
        while messages[j - 1].get("role") == "tool":
            j -= 1
        owner = messages[j - 1]
        case.assertEqual(owner.get("role"), "assistant", f"orphan tool result at {i}")
        case.assertLessEqual(i - j + 1, len(owner.get("tool_calls") or []),
                             f"tool result at {i} has no call")


class _Agent:
    """Fake ollama.chat: Reads the files round-robin, then finishes; raises past the ceiling."""

    def __init__(self, files: list[str], steps: int) -> None:
        self.files = files
        self.steps = steps
        self.done = 0
        self.peak = 0
        self.summaries = 0

    def __call__(self, *, model, messages, tools, on_delta=None, **kw):
        if tools is None:
            self.summaries += 1
            return {"role": "assistant", "content": "read the files, nothing edited"}
        est = context_budget.estimate_tokens(messages, tools)
        self.peak = max(self.peak, est)
        if est > NUM_CTX:
            raise RuntimeError(f"context window exceeded: {est} > {NUM_CTX}")
        step, self.done = self.done, self.done + 1
        if step < self.steps:
            reply = _call(f"c{step}", "Read", file_path=self.files[step % len(self.files)])
        else:
            reply = {"role": "assistant", "content": "done", "tool_calls": []}
        return {**reply, "_usage": dict(_USAGE)}


class TestContextBudgetSession(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.files = []
        for i in range(3):
            path = tmp_path / f"mod{i}.py"
            path.write_text("".join(f"value_{i}_{n} = '{'x' * 60}'\n" for n in range(50)))
            self.files.append(str(path))
        patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch("coding_harness.core.session.SESSIONS_DIR", tmp_path / "sessions"),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(allowed=True, reason="test", path="hook"),
            ),
            mock.patch.dict(os.environ, {"HARNESS_NUM_CTX": str(NUM_CTX)}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)
        self.events: list = []

    def _run(self, agent, **kw) -> tuple[Session, object]:
        """One turn with ``agent`` standing in for ollama.chat."""
        registry = build_registry(event_sink=None, enable_mcp=False)
        registry.confirm_callback = lambda _plan: True
        session = Session(
            model="mistral-small3.2:latest", registry=registry, system_prompt="sys",
            event_sink=self.events.append, force_local=True,
            verify_repair=False, agentic_review=kw.pop("agentic_review", False), **kw,
        )
        with mock.patch("coding_harness.core.session.ollama.chat", side_effect=agent):
            result = session.run_turn("survey the modules")
        return session, result

    def _payloads(self, kind: str) -> list[dict]:
        return [e.payload for e in self.events if e.kind == kind]

    def test_full_step_loop_runs_past_the_old_ceiling(self) -> None:
        agent = _Agent(self.files, MAX_TURNS - 1)
        session, result = self._run(agent)
        self.assertEqual(result.halted_reason, "model_done", result.error)
        self.assertEqual(result.turns, MAX_TURNS)
        self.assertLessEqual(agent.peak, NUM_CTX)
        elides = self._payloads("context_elide")
        self.assertTrue(elides)
        self.assertGreater(elides[0]["count"], 0)
        self.assertLess(elides[0]["tokens_after"], elides[0]["tokens_before"])
        compactions = self._payloads("compaction")
        self.assertTrue(compactions)
        self.assertTrue(all(c["mid_turn"] for c in compactions))
        self.assertEqual(session.messages[1], {"role": "user", "content": "survey the modules"})

    def test_kill_switch_hits_the_ceiling(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_CONTEXT_BUDGET": "0"}):
            _session, result = self._run(_Agent(self.files, MAX_TURNS - 1))
        self.assertEqual(result.halted_reason, "error")
        self.assertIn("context window exceeded", result.error or "")
        self.assertLess(result.turns, MAX_TURNS)
        self.assertEqual(self._payloads("context_elide"), [])

    def test_replay_reproduces_the_shrunk_history(self) -> None:
        session, _result = self._run(_Agent(self.files, MAX_TURNS - 1))
        rebuilt = replay.rebuild_messages(session.session_log_path, "sys")
        self.assertEqual(rebuilt.warnings, [])
        self.assertEqual(_normalized(rebuilt.messages), _normalized(session.messages))
        _assert_well_formed(self, rebuilt.messages)

    def test_replay_after_review_feedback_then_shrink(self) -> None:
        """A failed review feeds back a user message; a shrink right after keeps it in the tail."""
        target = str(Path(self.files[0]).with_name("new.py"))
        replies = iter([
            _call("w1", "Write", file_path=target, content="x = 1\n"),
            _call("r1", "Read", file_path=self.files[1]),
            _call("r2", "Read", file_path=self.files[2]),
            {"role": "assistant", "content": "done", "tool_calls": []},
            {"role": "assistant", "content": "fixed", "tool_calls": []},
        ])
        budget = {"tokens": 10**9}

        def _review(*_a, **_kw):
            budget["tokens"] = 1  # the very next local call must shrink
            return ReviewResult(approved=False, concerns="name it", backend="local", model="m")

        def _chat(**kw):
            if kw.get("tools") is None:
                return {"role": "assistant", "content": "summary"}
            return {**next(replies), "_usage": dict(_USAGE)}

        with mock.patch("coding_harness.core.review.review_change", side_effect=_review), \
                mock.patch.object(context_budget, "token_budget", lambda _n: budget["tokens"]):
            session, result = self._run(_chat, agentic_review=True)
        self.assertEqual(result.halted_reason, "model_done", result.error)
        compactions = self._payloads("compaction")
        self.assertEqual(len(compactions), 1)
        self.assertEqual([m["role"] for m in session.messages[-4:]],
                         ["assistant", "assistant", "user", "assistant"])
        rebuilt = replay.rebuild_messages(session.session_log_path, "sys")
        self.assertEqual(rebuilt.user_turns, 1)
        self.assertEqual(_normalized(rebuilt.messages), _normalized(session.messages))
        _assert_well_formed(self, rebuilt.messages)

    def test_grep_output_is_clamped(self) -> None:
        big = Path(self.files[0]).with_name("big.txt")
        big.write_text("".join(f"needle {n} {'y' * 150}\n" for n in range(200)))
        replies = iter([
            _call("g1", "Grep", pattern="needle", path=str(big), output_mode="content"),
            {"role": "assistant", "content": "done", "tool_calls": []},
        ])
        session, result = self._run(lambda **kw: {**next(replies), "_usage": dict(_USAGE)})
        self.assertEqual(result.halted_reason, "model_done", result.error)
        tool_msg = next(m for m in session.messages if m.get("role") == "tool")
        self.assertLessEqual(len(tool_msg["content"]), context_budget.TOOL_CAPS["Grep"])
        self.assertIn("[Grep result clamped:", tool_msg["content"])


if __name__ == "__main__":
    unittest.main()
