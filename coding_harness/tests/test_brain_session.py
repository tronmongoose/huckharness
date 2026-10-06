"""The Brain tool inside a session: tier caps, the sensitive-history pin, resume."""
from __future__ import annotations

import json
import signal
from unittest import mock

from coding_harness.context import brain
from coding_harness.core import replay
from coding_harness.core.session import Session
from coding_harness.modes.print_mode import build_registry
from coding_harness.tests import fake_brain
from coding_harness.tests.test_done_gate_session import (
    _Scripted,
    _SessionBase,
    _terminal,
)
from coding_harness.tools.brain import Brain


def _brain_call(query: str) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": "k1", "function": {"name": "Brain", "arguments": json.dumps({"query": query})}}]}


def _frontier(sensitive: bool = False):
    return mock.MagicMock(sensitivity_flag=sensitive, route="frontier",
                          classification={"route": "frontier"})


def test_frontier_turn_sees_only_internal_notes(tmp_path) -> None:
    tool = Brain(brain.BrainClient(fake_brain.block(tmp_path)))
    tool.local = False
    out = tool.run({"query": "larkspur"})
    assert "startup/plan.md" in out.content
    assert "finance/budget.md" not in out.content and "health/visit.md" not in out.content
    assert "3 more result(s) withheld" in out.content
    assert out.metadata == {"max_tier": 1, "withheld": 3}
    tool.client.close()


def test_local_turn_sees_every_tier(tmp_path) -> None:
    tool = Brain(brain.BrainClient(fake_brain.block(tmp_path)))
    out = tool.run({"query": "larkspur"})
    assert "finance/budget.md" in out.content and out.metadata["max_tier"] == 3
    tool.client.close()


def test_importing_mcp_leaves_broken_pipes_as_exceptions() -> None:
    import coding_harness.mcp.transport  # noqa: F401 — the import is what is under test

    assert signal.getsignal(signal.SIGPIPE) == signal.SIG_IGN


def test_replay_keeps_the_sensitive_mark(tmp_path) -> None:
    path = tmp_path / "t.jsonl"
    rows = [{"kind": "session_start", "session_id": "s", "model": "m"},
            {"kind": "user_message", "content": "q", "turn": 1},
            {"kind": "sensitive_context", "source": "brain_tool", "tier": 2},
            {"kind": "assistant_message", "turn": 1, "content": "a"}]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    assert replay.rebuild_messages(path, "sys").sensitive is True


class BrainSessionTests(_SessionBase):
    def _brain_session(self, scripted, **kw) -> Session:
        registry = build_registry(event_sink=None, enable_mcp=False,
                                  brain_block=fake_brain.block(self.root))
        session = Session(model="mistral-small3.2:latest", registry=registry,
                          system_prompt="terse", event_sink=self.events.append,
                          done_gate_enabled=False, verify_repair=False,
                          agentic_review=False, targeted_tests_enabled=False, **kw)
        patch = mock.patch("coding_harness.core.session.ollama.chat", side_effect=scripted)
        patch.start()
        self.addCleanup(patch.stop)
        return session

    def test_confidential_hit_pins_the_rest_of_the_session_local(self) -> None:
        session = self._brain_session(_Scripted([_brain_call("larkspur"), _terminal()]),
                                      force_local=True)
        session.run_turn("what is my budget?")
        assert session.sensitive_context is True
        marks = [e.payload for e in self.events if e.kind == "sensitive_context"]
        assert marks == [{"source": "brain_tool", "tier": 3}]
        with mock.patch("coding_harness.core.session.decide_route", return_value=_frontier()):
            assert session._decide_route_for_turn("hi", "claude-cli")[1] == "sensitivity_block"
            session.force_local = False
            assert session._decide_route_for_turn("hi", None)[1] == "sensitivity_block"

    def test_internal_only_hits_leave_frontier_routes_open(self) -> None:
        session = self._brain_session(_Scripted([_brain_call("launch"), _terminal()]),
                                      force_local=True)
        session.run_turn("what is the launch plan?")
        assert session.sensitive_context is False

    def test_brain_is_absent_without_a_block(self) -> None:
        assert "Brain" not in build_registry(event_sink=None, enable_mcp=False).tools
