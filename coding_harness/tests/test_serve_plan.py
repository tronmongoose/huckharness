"""Spec-then-run over serve: plan, edit, act, autonomy and todo routes.

A real server on an ephemeral port; the model is a fake that answers a plan
prompt with plan text and an act prompt with one TodoWrite call then prose.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from unittest import mock

import pytest

from coding_harness.core.mode import Autonomy
from coding_harness.core.paths import meta_dir
from coding_harness.core.session import Session, SessionResult
from coding_harness.modes import serve_mode, serve_plan
from coding_harness.modes.repl_plan import plan_path_for
from coding_harness.security import audit

_USAGE = {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None}
_PLAN = ("## Plan\n\n1. change widget.py so the widget takes a size argument\n"
         "2. update every caller of the widget to pass the size explicitly\n"
         "3. add a unit test for the default size and one for a custom size\n"
         "4. run the tests")
_TODO = [{"id": "1", "text": "edit the widget", "status": "in_progress"}]


class _Model:
    """Fake ollama.chat: records each call's prompt and visible tools."""

    def __init__(self):
        self.calls = []
        self.plan_reply = _PLAN
        self.gate = None  # a threading.Event holds every call until set
        self.fail = None  # an exception raised instead of replying

    def __call__(self, *, model, messages, tools, on_delta=None, **_kw):
        prompt = next(m["content"] for m in reversed(messages) if m["role"] == "user")
        names = sorted(t["function"]["name"] for t in tools or [])
        self.calls.append({"prompt": prompt, "tools": names, "messages": messages})
        if self.gate is not None:
            self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        if "Execute this plan" in prompt and messages[-1]["role"] == "user":
            call = {"function": {"name": "TodoWrite", "arguments": {"items": _TODO}}}
            return {"role": "assistant", "content": "", "tool_calls": [call], "_usage": _USAGE}
        text = self.plan_reply if "implementation plan" in prompt else "done"
        return {"role": "assistant", "content": text, "tool_calls": [], "_usage": _USAGE}


def _request(method, url, body=None):
    """(status, parsed JSON) for one call."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture()
def server():
    """(base url, fake model); Sentinel stubbed, MCP off."""
    model = _Model()
    verdict = mock.MagicMock(allowed=True, reason="test", path="hook")
    started = threading.Event()
    box = {}

    def _ready(srv):
        box["srv"] = srv
        started.set()

    with mock.patch("coding_harness.core.session.ollama.chat", side_effect=model), \
            mock.patch("coding_harness.tools.registry.sentinel.review", return_value=verdict):
        t = threading.Thread(target=serve_mode.run, daemon=True, kwargs=dict(
            host="127.0.0.1", port=0, enable_mcp=False, ready_callback=_ready))
        t.start()
        assert started.wait(5)
        srv = box["srv"]
        yield f"http://127.0.0.1:{srv.server_port}", model
        srv.shutdown()
        srv.server_close()
        t.join(2)


def _session(base, autonomy="medium"):
    """A new session id at ``autonomy``."""
    status, body = _request("POST", f"{base}/v1/sessions", {"autonomy": autonomy})
    assert status == 201
    return body["session_id"]


def _idle(base, sid):
    """Block until the session's turn has finished."""
    for _ in range(200):
        _, body = _request("GET", f"{base}/v1/sessions")
        row = next(s for s in body["sessions"] if s["session_id"] == sid)
        if not row["turn_active"]:
            return row
        time.sleep(0.02)
    raise AssertionError("turn never finished")


def test_plan_saves_under_meta_dir_and_restores_autonomy(server):
    base, model = server
    sid = _session(base)
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/plan", {"goal": "add a widget"})
    assert status == 202
    row = _idle(base, sid)
    assert row["autonomy"] == "medium" and row["mode"] == "act"
    assert "Write" not in model.calls[0]["tools"] and "Bash" not in model.calls[0]["tools"]
    assert "TodoWrite" in model.calls[0]["tools"]
    path = meta_dir() / "plans" / f"{sid}.md"
    assert path.read_text().strip() == model.plan_reply
    _, got = _request("GET", f"{base}/v1/sessions/{sid}/plan")
    assert got["exists"] and got["text"].strip() == model.plan_reply


def test_plan_without_text_saves_nothing(server):
    base, model = server
    model.plan_reply = ""
    sid = _session(base)
    _request("POST", f"{base}/v1/sessions/{sid}/plan", {"goal": "anything"})
    _idle(base, sid)
    _, got = _request("GET", f"{base}/v1/sessions/{sid}/plan")
    assert got["exists"] is False


def test_put_text_reaches_the_act_turn_and_todo(server):
    base, model = server
    sid = _session(base, "off")
    status, body = _request("PUT", f"{base}/v1/sessions/{sid}/plan", {"text": "EDITED PLAN"})
    assert status == 200 and body["bytes"] == len("EDITED PLAN")
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/act", {"autonomy": "low"})
    assert status == 202
    row = _idle(base, sid)
    assert row["autonomy"] == "low"
    first = model.calls[0]["prompt"]
    assert "EDITED PLAN" in first and "TodoWrite" in first
    _, todo = _request("GET", f"{base}/v1/sessions/{sid}/todo")
    assert todo["items"] == _TODO


def test_act_rejects_off_and_missing_plan(server):
    base, _ = server
    sid = _session(base)
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/act", {"autonomy": "low"})
    assert status == 400
    _request("PUT", f"{base}/v1/sessions/{sid}/plan", {"text": "a plan"})
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/act", {"autonomy": "off"})
    assert status == 400


def test_put_rejects_non_string(server):
    base, _ = server
    sid = _session(base)
    status, _ = _request("PUT", f"{base}/v1/sessions/{sid}/plan", {"text": 3})
    assert status == 400


def _rows(sid, kind):
    """Audit rows of ``kind`` for ``sid``."""
    lines = audit.AUDIT_PATH.read_text().splitlines()
    return [r for r in map(json.loads, lines) if r.get("kind") == kind and r.get("session_id") == sid]


def test_autonomy_route_audits_and_swaps_envelope(server):
    base, _ = server
    sid = _session(base, "off")
    _, env = _request("GET", f"{base}/v1/sessions/{sid}/envelope")
    assert not any(g["access"] == "write" for g in env["envelope"]["grants"])
    status, body = _request("POST", f"{base}/v1/sessions/{sid}/autonomy", {"level": "high"})
    assert status == 200 and body == {"session_id": sid, "from": "off", "to": "high", "mode": "act"}
    assert _rows(sid, "autonomy_change")[-1]["to_level"] == "high"
    assert _rows(sid, "envelope_change")[-1]["action"] == "preset"
    _, env = _request("GET", f"{base}/v1/sessions/{sid}/envelope")
    assert any(g["tool"] == "Bash" for g in env["envelope"]["grants"])
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/autonomy", {"level": "max"})
    assert status == 400


def test_custom_envelope_is_not_rebuilt(server):
    base, _ = server
    spec = {"grants": [{"tool": "Read", "access": "read"}]}
    status, body = _request("POST", f"{base}/v1/sessions", {"autonomy": "off", "envelope": spec})
    sid = body["session_id"]
    _request("POST", f"{base}/v1/sessions/{sid}/autonomy", {"level": "high"})
    _, env = _request("GET", f"{base}/v1/sessions/{sid}/envelope")
    assert [g["tool"] for g in env["envelope"]["grants"]] == ["Read"]


def _event(base, sid, method):
    """Params of the first ``method`` event on the stream (history replays)."""
    with urllib.request.urlopen(f"{base}/v1/sessions/{sid}/events", timeout=5) as resp:
        for _ in range(500):
            line = resp.readline().decode()
            if line.startswith("data: "):
                msg = json.loads(line[6:])
                if msg["method"] == method:
                    return msg["params"]
    raise AssertionError(f"no {method} event")


def _running_plan(base, model, autonomy="medium"):
    """A session whose plan turn is parked inside the model call."""
    model.gate = threading.Event()
    sid = _session(base, autonomy)
    assert _request("POST", f"{base}/v1/sessions/{sid}/plan", {"goal": "g"})[0] == 202
    for _ in range(200):
        if model.calls:
            return sid
        time.sleep(0.02)
    raise AssertionError("plan turn never reached the model")


def test_plan_turn_sees_exactly_the_off_surface(server):
    base, model = server
    sid = _session(base, "high")
    _request("POST", f"{base}/v1/sessions/{sid}/plan", {"goal": "g"})
    _idle(base, sid)
    assert set(model.calls[0]["tools"]) == {"Read", "Grep", "Glob", "TodoWrite"}


def test_autonomy_is_refused_mid_turn(server):
    base, model = server
    sid = _running_plan(base, model)
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/autonomy", {"level": "high"})
    assert status == 409
    model.gate.set()
    assert _idle(base, sid)["autonomy"] == "medium"
    assert all("Write" not in c["tools"] for c in model.calls)


def test_act_refused_mid_turn_leaves_the_level_alone(server):
    base, model = server
    sid = _running_plan(base, model)
    plan_path_for(sid).parent.mkdir(parents=True, exist_ok=True)
    plan_path_for(sid).write_text("a plan")
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/act", {"autonomy": "high"})
    assert status == 409
    model.gate.set()
    assert _idle(base, sid)["autonomy"] == "medium"


def test_act_raises_the_level_inside_the_turn(server):
    base, model = server
    sid = _session(base, "off")
    _request("PUT", f"{base}/v1/sessions/{sid}/plan", {"text": "a plan"})
    _request("POST", f"{base}/v1/sessions/{sid}/act", {"autonomy": "medium"})
    assert _idle(base, sid)["autonomy"] == "medium"
    assert "Write" in model.calls[0]["tools"]


def test_model_error_fails_the_plan_and_keeps_the_old_one(server):
    base, model = server
    model.fail = RuntimeError("ollama exploded")
    sid = _session(base)
    _request("PUT", f"{base}/v1/sessions/{sid}/plan", {"text": "OPERATOR PLAN"})
    _request("POST", f"{base}/v1/sessions/{sid}/plan", {"goal": "g"})
    assert "ollama exploded" in _event(base, sid, "plan_failed")["reason"]
    assert plan_path_for(sid).read_text() == "OPERATOR PLAN"


def test_halt_text_is_not_saved_as_a_plan(server):
    base, _ = server
    halted = SessionResult(final_text="(halted: interrupted by operator)", turns=1,
                           session_id="x", session_log_path=meta_dir() / "x.jsonl",
                           halted_reason="interrupted")
    sid = _session(base)
    _request("PUT", f"{base}/v1/sessions/{sid}/plan", {"text": "OPERATOR PLAN"})
    with mock.patch.object(Session, "run_turn", return_value=halted):
        _request("POST", f"{base}/v1/sessions/{sid}/plan", {"goal": "g"})
        _idle(base, sid)
    assert "interrupted" in _event(base, sid, "plan_failed")["reason"]
    assert plan_path_for(sid).read_text() == "OPERATOR PLAN"


def test_level_is_restored_when_run_turn_raises(server):
    base, _ = server
    sid = _session(base, "high")
    with mock.patch.object(Session, "run_turn", side_effect=RuntimeError("crash")):
        _request("POST", f"{base}/v1/sessions/{sid}/plan", {"goal": "g"})
        row = _idle(base, sid)
    assert row["autonomy"] == "high" and row["mode"] == "act"
    assert "crash" in _event(base, sid, "plan_failed")["reason"]


def test_resumed_session_envelope_is_never_rebuilt():
    state = serve_mode._ServerState(model="m", force_local=True, explicit_model=False,
                                    enable_mcp=False)
    sid = "resume-no-envelope"
    transcript = serve_mode.SESSIONS_DIR / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("")
    replayed = mock.MagicMock(complete=True, envelope=None, mode=None, agent_identity=None,
                              messages=[], user_turns=0)
    with mock.patch.object(serve_mode, "rebuild_messages", return_value=replayed):
        entry, _ = state.resume_session(sid)
    assert entry is not None and entry.envelope_preset is False
    before = [g.to_dict() for g in entry.envelope.grants]
    serve_plan.apply_autonomy(entry, Autonomy.HIGH, "test", state.settings)
    assert [g.to_dict() for g in entry.envelope.grants] == before
    state.close()


def test_an_apology_is_not_saved_as_a_plan(server):
    base, model = server
    model.plan_reply = "Sorry, I cannot help with that request. " * 8
    sid = _session(base)
    _request("PUT", f"{base}/v1/sessions/{sid}/plan", {"text": "OPERATOR PLAN"})
    _request("POST", f"{base}/v1/sessions/{sid}/plan", {"goal": "g"})
    assert _event(base, sid, "plan_failed")["reason"] == "not_a_plan"
    assert plan_path_for(sid).read_text() == "OPERATOR PLAN"


def test_plan_shape_needs_structure_and_length():
    assert serve_plan.looks_like_plan(_PLAN)
    assert serve_plan.looks_like_plan("# Title\n" + "x" * 200)
    assert not serve_plan.looks_like_plan("- short list")
    assert not serve_plan.looks_like_plan("no structure here " * 20)
