"""Memory proposals: strict parsing, the post-turn pass, and every operator decision."""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import pytest

from coding_harness.context import memory
from coding_harness.core import memory_proposals as mp
from coding_harness.core import memory_store as ms
from coding_harness.core import model_roles, paths
from coding_harness.core.hooks import HookDecision
from coding_harness.core.router import RouteDecision
from coding_harness.core.turn_loop import TurnState

BODY = ("Run the per-file test target, never the whole suite in one process.\n\n"
        "**Why:** a teardown SIGPIPE killed whole-suite runs.\n\n"
        "**How to apply:** use make test or pytest on one file.")


def _cand(**over) -> dict:
    return {"name": "per-file-tests", "description": "Run tests one file at a time",
            "type": "feedback", "body": BODY, **over}


def _reply(items) -> str:
    return json.dumps({"memories": items})


@pytest.fixture
def mem_dir(tmp_path, monkeypatch):
    """Point the memory root under the temp meta dir and hide a deployment's indexer."""
    root = paths.meta_dir() / f"mem-{tmp_path.name}"
    monkeypatch.setenv("HARNESS_MEMORY_DIR", str(root))
    monkeypatch.setattr(ms.importlib, "import_module", mock.Mock(side_effect=ImportError))
    return memory.memory_dir(str(tmp_path))


def test_parse_accepts_a_valid_reply() -> None:
    assert mp.parse_candidates(_reply([_cand()])) == [_cand()]
    assert mp.parse_candidates("<think>x</think>```json\n[]\n```") == []


@pytest.mark.parametrize("text", [
    "not json",
    _reply([_cand(type="opinion")]),
    _reply([_cand()] * 4),
    _reply([{**_cand(), "extra": "x"}]),
    _reply([_cand(name="Bad Name")]),
    _reply([_cand(body="too short, no why")]),
    _reply([_cand(description="two\nlines here")]),
    json.dumps({"verdict": "ok"}),
])
def test_parse_rejects_malformed_replies(text: str) -> None:
    with pytest.raises(ValueError):
        mp.parse_candidates(text)


def _state(**kw) -> TurnState:
    base = dict(user_prompt="p", selected_model="m", route_reason="complexity_low",
                decision=None, tools=[], gate=mock.MagicMock(), cwd=".")
    return TurnState(**{**base, **kw})


def _session(messages, *, propose=True) -> mock.MagicMock:
    s = mock.MagicMock(session_id="sid-proposals", model="mistral-small3.2", _user_turn=1,
                       _turn_anchor=1, propose_memories=propose, sensitive_context=False)
    s._messages = [{"role": "system", "content": "s"}, *messages]
    return s


CORRECTED = [{"role": "user", "content": "do x"}, {"role": "assistant", "content": "did y"},
             {"role": "user", "content": "no, x"}]
DONE = SimpleNamespace(halted_reason="model_done")


def test_signals() -> None:
    assert mp.signals(CORRECTED, _state(), DONE) == ["correction"]
    denied = [{"role": "user", "content": "q"}, {"role": "tool", "name": "Bash",
                                                 "content": "BLOCKED by envelope: x"}]
    assert mp.signals(denied, _state(), DONE) == ["denial"]
    assert mp.signals(denied[:1], _state(repair_rounds=1), DONE) == ["fixed_check"]
    assert mp.signals(denied[:1], _state(recall_block="## Recalled notes\n"), DONE) == ["brain"]
    assert mp.signals(denied[:1], _state(), DONE) == []


def _decide(sid: str, meta: dict, decision: str, text=None, cwd=".", **kw) -> dict:
    """Decide as a card that showed the proposal's current text."""
    return ms.decide(sid, meta["id"], decision, text, cwd, seen=meta["sha256"], **kw)


def test_after_turn_writes_proposals_and_emits(monkeypatch) -> None:
    session = _session(CORRECTED)
    monkeypatch.setattr(mp.review, "_tag_present", lambda m: True)
    with mock.patch.object(mp.ollama, "chat", return_value={"content": _reply([_cand()])}) as chat:
        worker = mp.after_turn(session, _state(), DONE)
        worker.join(5)
    # Proposals are summarizing, so they ride the small summarize-role model.
    assert chat.call_args.kwargs["model"] == model_roles.DEFAULTS["summarize"]
    kind, meta = session._emit.call_args.args
    assert kind == "memory_proposed" and session._emit.call_count == 1
    assert {k: meta[k] for k in ("name", "description", "type")} == {
        "name": "per-file-tests", "description": "Run tests one file at a time", "type": "feedback"}
    rows = ms.list_proposals("sid-proposals")
    assert rows[0]["id"] == meta["id"] and rows[0]["sha256"] == meta["sha256"]
    assert "provenance: generated" in rows[0]["text"]
    assert ms.proposals_dir("sid-proposals").resolve().is_relative_to(paths.meta_dir().resolve())
    _decide("sid-proposals", meta, "reject")


@pytest.mark.parametrize("state_kw,propose,env", [
    ({"route_reason": "complexity_high"}, True, "1"),
    ({"use_cli": True}, True, "1"),
    ({}, False, "1"),
    ({}, True, "0"),
])
def test_after_turn_never_runs_off_local_or_when_off(monkeypatch, state_kw, propose, env) -> None:
    monkeypatch.setenv("HARNESS_MEMORY_PROPOSALS", env)
    with mock.patch.object(mp.ollama, "chat") as chat:
        assert mp.after_turn(_session(CORRECTED, propose=propose), _state(**state_kw), DONE) is None
    chat.assert_not_called()


def test_a_sensitive_session_or_prompt_drafts_nothing() -> None:
    sensitive = _session(CORRECTED)
    sensitive.sensitive_context = True
    flagged = _session(CORRECTED)
    flagged.sensitive_context = False
    decision = RouteDecision(sensitivity_flag=True)
    with mock.patch.object(mp.ollama, "chat") as chat:
        assert mp.after_turn(sensitive, _state(), DONE) is None
        assert mp.after_turn(flagged, _state(decision=decision), DONE) is None
    chat.assert_not_called()
    sensitive._log.assert_called_once_with(
        "memory_proposal_skipped", {"turn": 1, "reason": "sensitive"})


def test_a_bad_generator_reply_stores_nothing(monkeypatch) -> None:
    monkeypatch.setattr(mp.review, "_tag_present", lambda m: True)
    session = _session(CORRECTED)
    session.session_id = "sid-bad"
    with mock.patch.object(mp.ollama, "chat", return_value={"content": "nope"}):
        mp.after_turn(session, _state(), DONE).join(5)
    assert ms.list_proposals("sid-bad") == []
    session._emit.assert_not_called()


def test_approve_lands_the_file_and_indexes_it(tmp_path, mem_dir) -> None:
    meta = ms.write_proposal("sid-a", _cand(), 2)
    events = []
    out = _decide("sid-a", meta, "approve", cwd=str(tmp_path),
                  emit=lambda k, p: events.append((k, p)))
    landed = mem_dir / "feedback_per_file_tests.md"
    assert out == {"id": meta["id"], "decision": "approve", "path": str(landed)}
    assert "metadata:\n  type: feedback\n  provenance: generated" in landed.read_text()
    assert "- [per-file-tests](feedback_per_file_tests.md) — Run tests one file at a time" in (
        mem_dir / "MEMORY.md").read_text()
    assert not list(mem_dir.glob("*.tmp"))
    assert events == [("memory_decided", out)]
    assert ms.list_proposals("sid-a") == []
    again = ms.write_proposal("sid-a", _cand(), 2)
    with pytest.raises(ms.ProposalError) as err:
        _decide("sid-a", again, "approve", cwd=str(tmp_path))
    assert err.value.status == 409 and "already exists" in str(err.value)


def test_edit_replaces_the_text_then_approves(tmp_path, mem_dir) -> None:
    meta = ms.write_proposal("sid-e", _cand(), 3)
    text = ms.render(_cand(name="edited-name", body=BODY + "\nMore."), "generated", "sid-e", 3)
    _decide("sid-e", meta, "edit", text, cwd=str(tmp_path))
    landed = (mem_dir / "feedback_edited_name.md").read_text()
    assert "provenance: operator" in landed and landed.rstrip().endswith("More.")
    bad = ms.write_proposal("sid-e", _cand(), 3)
    with pytest.raises(ms.ProposalError) as err:
        _decide("sid-e", bad, "edit", "no frontmatter", cwd=str(tmp_path))
    assert err.value.status == 400


def test_reject_deletes_and_nothing_lands_without_a_decision(tmp_path, mem_dir) -> None:
    meta = ms.write_proposal("sid-r", _cand(), 1)
    assert not mem_dir.exists()
    assert _decide("sid-r", meta, "reject") == {"id": meta["id"], "decision": "reject"}
    assert ms.list_proposals("sid-r") == [] and not mem_dir.exists()
    with pytest.raises(ms.ProposalError) as err:
        _decide("sid-r", meta, "approve", cwd=str(tmp_path))
    assert err.value.status == 404


def test_ids_are_never_reused_and_writers_do_not_collide() -> None:
    first = ms.write_proposal("sid-u", _cand(), 1)
    _decide("sid-u", first, "reject")
    second = ms.write_proposal("sid-u", _cand(), 1)
    assert second["id"] != first["id"]
    with pytest.raises(ms.ProposalError) as err:
        _decide("sid-u", first, "approve")
    assert err.value.status == 404  # the stale card's id is gone, never re-pointed
    ids = {ms.write_proposal("sid-u", _cand(), 1)["id"] for _ in range(50)}
    assert len(ids) == 50 and len(ms.list_proposals("sid-u")) == 51
    listed = [r["id"] for r in ms.list_proposals("sid-u")]
    assert listed == sorted(listed) and listed[0] == second["id"]


def test_a_changed_or_missing_hash_is_refused(tmp_path, mem_dir) -> None:
    meta = ms.write_proposal("sid-m", _cand(), 1)
    path = ms.proposals_dir("sid-m") / f"{meta['id']}.md"
    path.write_text(path.read_text().replace("Run tests", "Skip tests"))
    for seen in (meta["sha256"], None, "x"):
        with pytest.raises(ms.ProposalError) as err:
            ms.decide("sid-m", meta["id"], "approve", None, str(tmp_path), seen=seen)
        assert err.value.status == 409
    assert not mem_dir.exists()
    fresh = ms.list_proposals("sid-m")[0]["sha256"]
    ms.decide("sid-m", meta["id"], "reject", None, ".", seen=fresh)


def test_a_hook_denial_keeps_the_proposal(tmp_path, mem_dir) -> None:
    meta = ms.write_proposal("sid-h", _cand(), 1)
    hooks = mock.MagicMock()
    hooks.run.return_value = HookDecision(allowed=False, reason="schema: bad")
    with pytest.raises(ms.ProposalError) as err:
        _decide("sid-h", meta, "approve", cwd=str(tmp_path), hooks=hooks)
    assert err.value.status == 422 and "schema: bad" in str(err.value)
    event, = hooks.run.call_args_list
    assert event.args == ("PreToolUse",) and event.kwargs["tool_name"] == "Write"
    assert event.kwargs["tool_input"]["file_path"].endswith("feedback_per_file_tests.md")
    assert not mem_dir.exists() and len(ms.list_proposals("sid-h")) == 1


def test_ids_are_checked_at_the_boundary() -> None:
    for sid in ("../x", "a/b", ""):
        with pytest.raises(ms.ProposalError):
            ms.proposals_dir(sid)
    with pytest.raises(ms.ProposalError):
        ms.decide("sid-x", "../1", "approve", None, ".", seen="x")
    with pytest.raises(ms.ProposalError):
        ms.decide("sid-x", "1", "maybe", None, ".", seen="x")


def test_a_follow_up_prompt_with_a_correction_cue_qualifies() -> None:
    prompt = [{"role": "user", "content": "no, use tabs"}]
    for text in ("no, use tabs", "Actually keep it", "wrong file", "use pytest not unittest"):
        assert mp.signals(prompt, _state(user_prompt=text), DONE, prior_turn=True) == ["correction"]
    assert mp.signals(prompt, _state(user_prompt="no, use tabs"), DONE) == []
    assert mp.signals(prompt, _state(user_prompt="add a test"), DONE, prior_turn=True) == []
    assert mp.signals(prompt, _state(user_prompt="notes please"), DONE, prior_turn=True) == []


def test_after_turn_counts_the_previous_turn() -> None:
    session = _session([{"role": "user", "content": "instead, x"}])
    session._user_turn = 2
    with mock.patch.object(mp, "_work") as work:
        worker = mp.after_turn(session, _state(user_prompt="instead, x"), DONE)
        assert worker is not None
        worker.join(5)
        session._user_turn = 1
        assert mp.after_turn(session, _state(user_prompt="instead, x"), DONE) is None
    work.assert_called_once()


@pytest.mark.parametrize("brain_flag,block,expected", [
    (True, {}, True), (False, {"command": "recall"}, True), (False, {}, False),
])
def test_serve_proposes_when_a_brain_is_configured(brain_flag, block, expected) -> None:
    from coding_harness.core.mode import Mode
    from coding_harness.core.settings import Settings
    from coding_harness.modes import serve_mode
    state = serve_mode._ServerState(model="m", force_local=True, explicit_model=False,
                                    enable_mcp=False, settings=Settings(brain=block),
                                    brain=brain_flag)
    try:
        entry = state.create_session(mode=Mode.PLAN)
        assert entry.session.propose_memories is expected
    finally:
        state.close()
