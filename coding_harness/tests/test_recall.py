"""Prompt-time recall, pinned notes and the skill scorer, over the fake index."""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from coding_harness.context import brain, recall
from coding_harness.context.brain import Hit
from coding_harness.context.skills import Skill, score_skills
from coding_harness.core import replay
from coding_harness.core.router import RouteDecision
from coding_harness.core.session import Session
from coding_harness.modes.print_mode import build_registry
from coding_harness.tests import fake_brain
from coding_harness.tests.test_done_gate_session import _SessionBase, _terminal


class _Capture:
    """A scripted chat that records the messages each call received."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, *, model, messages, tools=None, **_kw):
        self.calls.append([dict(m) for m in messages])
        return self.replies[len(self.calls) - 1]


def _hit(path: str, tier: str, text: str = "x") -> Hit:
    return Hit(path, tier, 1.0, text)


def test_render_hits_stays_under_the_budget() -> None:
    hits = [_hit(f"v/n{i}.md", "internal", "word " * 400) for i in range(5)]
    block, kept = recall.render_hits(hits)
    assert block.startswith("## Recalled notes")
    assert len(block.encode("utf-8")) <= recall.RECALL_BUDGET
    assert kept and kept[0].path == "v/n0.md"


def test_render_pinned_respects_the_budget() -> None:
    pinned = [{"path": "a.md", "tier": 1, "content": "z" * 9000}]
    text = recall.render_pinned(pinned, 1000)
    assert text.startswith("## Pinned notes") and len(text.encode()) <= 1000


@pytest.mark.parametrize("prompt", ["/skill foo run the thing", "fix it", "   short   "])
def test_short_prompts_and_slash_commands_skip_search(prompt: str) -> None:
    assert recall.should_search(prompt) is False


def test_score_skills_ranks_and_thresholds() -> None:
    skills = [
        Skill("pdf", "Read and merge PDF files", Path("a")),
        Skill("code-review", "Review a diff for correctness bugs", Path("b")),
        Skill("dataviz", "Charts and plots for dashboards", Path("c")),
    ]
    ranked = score_skills("please review this diff for bugs", skills)
    assert [s.name for s, _ in ranked] == ["code-review"]
    assert score_skills("merge files", skills)[0][0].name == "pdf"  # two overlapping words
    assert score_skills("the pdf", skills) == []  # one word, name too short to count alone
    named = score_skills("run code-review now", skills)
    assert named[0][0].name == "code-review" and named[0][1] >= 3
    assert len(score_skills("review diff bugs charts plots dashboards merge pdf files", skills)) == 3


_DESIGN = Skill(
    "design-tenets",
    "House design tenets for interfaces that read as human-made, not AI-generated. "
    "Use whenever designing or restyling any web page, site, dashboard, or "
    "document-rendered artifact (PDF, brief).",
    Path("d"),
)


def test_an_unrelated_task_suggests_nothing() -> None:
    assert score_skills("remove the unused import in util.py", [_DESIGN]) == []
    leak = ("In the working directory, the file widget.py imports a module it never "
            "uses, so it fails lint. Remove the unused import line. Do not change any "
            "other behavior.")
    assert score_skills(leak, [_DESIGN]) == []


def test_design_tenets_prompt_suggests_the_skill() -> None:
    ranked = score_skills("apply the design tenets to this page", [_DESIGN])
    assert [s.name for s, _ in ranked] == ["design-tenets"]


class RecallSessionTests(_SessionBase):
    def _session(self, replies, **kw) -> tuple[Session, _Capture]:
        registry = build_registry(event_sink=None, enable_mcp=False,
                                  brain_block=fake_brain.block(self.root))
        session = Session(model="mistral-small3.2:latest", registry=registry,
                          system_prompt="terse", event_sink=self.events.append,
                          done_gate_enabled=False, verify_repair=False,
                          agentic_review=False, targeted_tests_enabled=False,
                          force_local=True, **kw)
        chat = _Capture(replies)
        for patch in (mock.patch("coding_harness.core.session.ollama.chat", side_effect=chat),
                      mock.patch.object(recall, "index_skills", return_value=[])):
            patch.start()
            self.addCleanup(patch.stop)
        return session, chat

    def _kinds(self, kind: str) -> list[dict]:
        return [e.payload for e in self.events if e.kind == kind]

    def test_a_confidential_hit_rides_the_turn_and_marks_sensitive(self) -> None:
        session, chat = self._session([_terminal()])
        session.run_turn("Larkspur budget")
        sent = chat.calls[0]
        assert sent[2]["content"] == "Larkspur budget"
        assert sent[1]["role"] == "user" and "finance/budget.md" in sent[1]["content"]
        assert session.sensitive_context is True
        assert self._kinds("sensitive_context") == [{"source": "recall", "tier": 2}]
        primed = self._kinds("brain_primed")[0]
        assert primed["paths"] == ["finance/budget.md"] and primed["tiers"] == [2]
        assert not any("## Recalled notes" in str(m.get("content")) for m in session.messages)

    def test_a_matching_skill_adds_one_hint_line(self) -> None:
        session, chat = self._session([_terminal()])
        session.recall_hints = True
        skill = Skill("code-review", "Review a diff for correctness bugs", Path("x"))
        with mock.patch.object(recall, "index_skills", return_value=[skill]):
            session.run_turn("please review my diff for bugs")
        assert "Consider /skill code-review for this task." in chat.calls[0][1]["content"]
        assert self._kinds("skills_suggested") == [{"names": ["code-review"]}]

    def _hinted(self, prompt: str, *, hints: bool, env: dict | None = None) -> list:
        """Run one turn against a design-tenets skill; the messages the model got."""
        session, chat = self._session([_terminal()])
        session.recall_hints = hints
        with mock.patch.object(recall, "index_skills", return_value=[_DESIGN]), \
                mock.patch.dict("os.environ", env or {}):
            session.run_turn(prompt)
        return chat.calls[0]

    def test_a_print_mode_session_gets_no_skill_hint(self) -> None:
        sent = self._hinted("apply the design tenets to this page", hints=False)
        assert not any("Consider /skill" in str(m.get("content")) for m in sent)
        assert self._kinds("skills_suggested") == []

    def test_kill_switch_stops_the_skill_hint(self) -> None:
        sent = self._hinted("apply the design tenets to this page", hints=True,
                            env={"HARNESS_RECALL": "0"})
        assert not any("Consider /skill" in str(m.get("content")) for m in sent)

    def test_a_hinted_session_gets_the_design_hint(self) -> None:
        sent = self._hinted("apply the design tenets to this page", hints=True)
        assert any("Consider /skill design-tenets" in str(m.get("content")) for m in sent)

    def test_recall_is_once_per_turn_and_replay_is_unchanged(self) -> None:
        call = {"role": "assistant", "content": "", "tool_calls": [{
            "id": "r1", "function": {"name": "Read", "arguments": '{"file_path": "mod.py"}'}}]}
        session, chat = self._session([call, _terminal()])
        with mock.patch.object(session.registry.tools["Brain"].client, "search",
                               wraps=session.registry.tools["Brain"].client.search) as spy:
            session.run_turn("Larkspur launch plan")
        assert spy.call_count == 1
        assert all("startup/plan.md" in c[1]["content"] for c in chat.calls)
        rebuilt = replay.rebuild_messages(session.session_log_path, "terse")
        assert rebuilt.user_turns == 1
        assert [m["role"] for m in rebuilt.messages] == [m["role"] for m in session.messages]
        assert session.sensitive_context is False

    def test_slash_prompts_do_not_search(self) -> None:
        session, _ = self._session([_terminal()])
        with mock.patch.object(session.registry.tools["Brain"].client, "search") as spy:
            session.run_turn("/skill anything at all here")
        spy.assert_not_called()

    def test_kill_switch(self) -> None:
        session, chat = self._session([_terminal()])
        with mock.patch.dict("os.environ", {"HARNESS_RECALL": "0"}):
            session.run_turn("Larkspur budget")
        assert len(chat.calls[0]) == 2 and not self._kinds("brain_primed")

    def test_tier_filter_drops_what_the_cap_forbids(self) -> None:
        session, chat = self._session([_terminal()])
        with mock.patch.object(recall, "allowed_tier", return_value=1):
            session.run_turn("Larkspur budget")
        assert len(chat.calls[0]) == 2 and session.sensitive_context is False

    def test_frontier_turns_never_recall(self) -> None:
        session, _ = self._session([])
        session.force_local = False
        route = RouteDecision()
        with mock.patch("coding_harness.core.session.decide_route", return_value=route), \
                mock.patch("coding_harness.core.session.claude_cli.chat",
                           return_value=_terminal()) as cli, \
                mock.patch.object(session.registry.tools["Brain"].client, "search") as spy:
            session.run_turn("Larkspur budget", model_override="claude-cli")
        spy.assert_not_called()
        sent = cli.call_args.kwargs["messages"]
        assert sent is session.messages and not self._kinds("brain_primed")

    def test_pin_enforces_tier_and_renders_each_turn(self) -> None:
        session, chat = self._session([_terminal(), _terminal()])
        note = recall.pin_note(session, session.registry.tools["Brain"].client, "finance/budget.md")
        assert note["tier"] == 2 and session.sensitive_context is True
        assert self._kinds("sensitive_context") == [{"source": "pin", "tier": 2}]
        session.run_turn("hello there, general")
        session.run_turn("and again, general")
        for call in chat.calls:
            pinned = [m for m in call if "## Pinned notes" in m["content"]]
            assert len(pinned) == 1 and "1200 a month" in pinned[0]["content"]
            assert call[call.index(pinned[0]) + 1]["content"].endswith("general")
        with pytest.raises(brain.BrainError):
            recall.pin_note(session, session.registry.tools["Brain"].client, "nope.md")
