"""The Explore subagent and chat/code session kinds."""
from __future__ import annotations

import json
from unittest import mock

from coding_harness.core import model_roles
from coding_harness.core.settings import Settings
from coding_harness.modes.print_mode import build_registry
from coding_harness.tests.test_done_gate_session import (
    _Scripted,
    _SessionBase,
    _terminal,
)
from coding_harness.tools.explore import Explore


def _call(name: str, args: dict, cid: str = "e1") -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": cid, "function": {"name": name, "arguments": json.dumps(args)}}]}


class ExploreKindTests(_SessionBase):
    def setUp(self) -> None:
        super().setUp()
        model_roles.configure(Settings())
        p = mock.patch.object(model_roles, "_present", lambda tag: True)
        p.start()
        self.addCleanup(p.stop)

    def _chat(self, script):
        seen: list = []

        def fake(*, model, messages, tools, **_kw):
            seen.append({"model": model, "tools": sorted(t["function"]["name"] for t in tools or [])})
            return script.pop(0)

        p = mock.patch("coding_harness.core.session.ollama.chat", side_effect=fake)
        p.start()
        self.addCleanup(p.stop)
        return seen

    def test_explore_child_runs_read_only_on_the_explore_model(self) -> None:
        seen = self._chat([_call("Grep", {"pattern": "x = 1"}), _terminal("x is set in mod.py:1")])
        out = Explore(Settings()).run({"task": "where is x set?"})
        assert seen[0] == {"model": model_roles.SMALL, "tools": ["Glob", "Grep", "Read"]}
        assert "x is set in mod.py:1" in out.content and out.metadata["steps"] == 2
        assert not out.is_error

    def test_explore_is_offered_only_when_asked_for(self) -> None:
        assert "Explore" not in build_registry(event_sink=None, enable_mcp=False).tools
        assert "Explore" in build_registry(event_sink=None, enable_mcp=False, subagents=True).tools

    def test_a_chat_session_sends_no_tools_and_uses_the_chat_model(self) -> None:
        session = self._session(_Scripted([]), done_gate_enabled=False)
        seen = self._chat([_terminal("hello")])  # patched after, so it wins
        session.set_kind("chat")
        session.run_turn("hi")
        assert seen == [{"model": model_roles.SMALL, "tools": []}]
        assert [e.payload for e in self.events if e.kind == "session_kind"] == [
            {"kind": "chat", "model": model_roles.SMALL}]
        session.set_kind("code")
        assert session.model == "mistral-small3.2:latest"


class ExploreConfinementTests(ExploreKindTests):
    def test_the_helper_cannot_read_outside_the_project(self) -> None:
        script = [_call("Read", {"file_path": "/etc/hosts"}),
                  _call("Grep", {"pattern": "root", "path": "/"}, cid="e2"),
                  _terminal("could not read it")]
        tool_msgs: list = []

        def fake(*, model, messages, tools, **_kw):
            tool_msgs[:] = [m["content"] for m in messages if m.get("role") == "tool"]
            return script.pop(0)

        with mock.patch("coding_harness.core.session.ollama.chat", side_effect=fake):
            out = Explore(Settings()).run({"task": "read /etc/hosts"})
        assert len(tool_msgs) == 2
        for content in tool_msgs:
            assert "out_of_envelope" in content or "DENIED" in content.upper(), content
        assert "localhost" not in " ".join(tool_msgs)
        assert "could not read it" in out.content


class ExploreProgressTests(ExploreKindTests):
    def test_child_progress_reaches_the_parent_as_names_only(self) -> None:
        self._chat([_call("Grep", {"pattern": "x = 1"}),
                    _call("Read", {"file_path": self.mod}, cid="e2"),
                    _terminal("x is set in mod.py:1")])
        tool = Explore(Settings())
        sent: list = []
        tool.emit = lambda kind, payload: sent.append((kind, payload))
        tool.run({"task": "where is x set?"})
        kinds = [k for k, _ in sent]
        assert kinds == ["subagent_start", "subagent_step", "subagent_step", "subagent_done"]
        start, done = sent[0][1], sent[-1][1]
        assert start["agent"] == "explore" and start["task"] == "where is x set?"
        assert {p["id"] for _, p in sent} == {start["id"]}
        assert [(p["tool"], p["summary"]) for k, p in sent if k == "subagent_step"] == [
            ("Grep", "x = 1"), ("Read", "mod.py")]  # paths inside the project read relative
        assert done == {"id": start["id"], "steps": 3, "halted": "model_done"}
        assert "x is set" not in json.dumps(sent), "the child's prose stays in the child"


def test_scout_transcripts_stay_out_of_history_and_continue(tmp_path, monkeypatch) -> None:
    from coding_harness.core import session as session_mod
    from coding_harness.core import transcripts

    monkeypatch.setattr(session_mod, "SESSIONS_DIR", tmp_path)
    for sid, extra in (("20260101T000000-aaaaaaaa", {}),
                       ("20260101T000001-bbbbbbbb", {"subagent": "explore"})):
        start = {"kind": "session_start", "session_id": sid, "cwd": "/w", **extra}
        (tmp_path / f"{sid}.jsonl").write_text(json.dumps(start) + "\n")
    assert [p.stem for p in transcripts.recent("/w", limit=10)] == ["20260101T000000-aaaaaaaa"]
