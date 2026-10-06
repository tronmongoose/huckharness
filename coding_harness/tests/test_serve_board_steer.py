"""Steer lifecycle: the final-read branch, drops at turn end, and who counts as running.

Reuses the gated fake model and live server from test_serve_board.
"""
from __future__ import annotations

import json
import threading
import urllib.error
from unittest import mock

import pytest

from coding_harness.core.session import Session
from coding_harness.security import sentinel
from coding_harness.tests.test_serve_board import (
    _USAGE,
    _live_entries,
    _request,
    _row,
    _session,
    _wait_status,
    serve_fake,
)
from coding_harness.tools.registry import ToolRegistry
from coding_harness.tools.write import Write


@pytest.fixture()
def board_server():
    """A live server over the gated fake model."""
    yield from serve_fake()


def _entry(sid):
    """The live _SessionEntry for ``sid``."""
    return next(e for e in _live_entries() if e.session.session_id == sid)


def test_steer_during_final_read_buys_another_step(board_server):
    base, model = board_server
    sid = _session(base)
    model.gate, model.hold = threading.Event(), 2  # hold the second (final) read
    _request("POST", f"{base}/v1/sessions/{sid}/turn", {"message": "fix it"})
    for _ in range(250):
        if len(model.calls) >= 2:
            break
        threading.Event().wait(0.02)
    assert len(model.calls) == 2
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/steer", {"message": "one more"})
    model.gate.set()
    assert status == 202
    assert _wait_status(base, sid, "done")["status"] == "done"
    assert model.calls[-1][-1] == {"role": "user", "content": "one more", "_steer": True}


def test_steer_during_compact_is_409_and_not_running(board_server):
    base, _model = board_server
    sid = _session(base)
    entry = _entry(sid)
    with entry.listeners_lock:
        entry.turn_active = True  # what /compact and revert hold, with no turn_id
    try:
        status, _ = _request("POST", f"{base}/v1/sessions/{sid}/steer", {"message": "x"})
        assert status == 409
        assert _row(base, sid)["status"] == "idle"
    finally:
        with entry.listeners_lock:
            entry.turn_active = False


def test_wait_turn_counts_as_running_and_steerable(board_server):
    base, model = board_server
    sid = _session(base)
    model.gate = threading.Event()
    box = {}
    t = threading.Thread(target=lambda: box.update(r=_request(
        "POST", f"{base}/v1/sessions/{sid}/turn", {"message": "go", "wait": True})))
    t.start()
    assert model.entered.wait(5)
    assert _row(base, sid)["status"] == "running"
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/steer", {"message": "also y"})
    assert status == 202
    model.gate.set()
    t.join(10)
    assert box["r"][0] == 200
    assert _row(base, sid)["status"] == "done"


def test_wait_turn_that_raises_records_an_error(board_server):
    base, _model = board_server
    sid = _session(base)
    entry = _entry(sid)
    with mock.patch.object(entry.session, "run_turn", side_effect=RuntimeError("boom")):
        try:
            _request("POST", f"{base}/v1/sessions/{sid}/turn", {"message": "go", "wait": True})
        except (urllib.error.URLError, ConnectionError):
            pass  # the handler re-raises; http.server drops the connection
    row = _row(base, sid)
    assert row["status"] == "error"
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/steer", {"message": "x"})
    assert status == 409


def test_steer_left_at_turn_end_is_dropped_not_carried(tmp_path):
    events = []
    reg = ToolRegistry()
    reg.register(Write())
    reg.confirm_callback = lambda _plan: True
    session = Session(model="fake-model", registry=reg, system_prompt="be terse",
                      agentic_review=False, event_sink=events.append)
    call = {"id": "w1", "function": {"name": "Write", "arguments": json.dumps(
        {"file_path": str(tmp_path / "a.txt"), "content": "hi"})}}
    seen = []

    def _chat(*, messages, **_kw):
        seen.append([dict(m) for m in messages])
        if len(seen) == 1:
            session.steer("too late")
            session.interrupt()  # the turn unwinds before the next model call
            return {"role": "assistant", "content": "", "tool_calls": [call], "_usage": _USAGE}
        return {"role": "assistant", "content": "ok", "tool_calls": [], "_usage": _USAGE}

    allow = sentinel.SentinelVerdict(allowed=True, reason="ok", path="hook")
    with mock.patch("coding_harness.core.session.ollama.chat", side_effect=_chat), \
            mock.patch.object(sentinel, "review", return_value=allow):
        assert session.run_turn("write a").halted_reason == "interrupted"
        dropped = [e.payload for e in events if e.kind == "steer_dropped"]
        assert dropped == [{"turn": 1, "texts": ["too late"]}]
        assert session.steer("between turns") is False
        session.run_turn("next")
    assert not any(m.get("_steer") for m in seen[-1])


def test_steer_closes_when_run_turn_raises():
    events = []
    session = Session(model="fake-model", registry=ToolRegistry(), system_prompt="be terse",
                      agentic_review=False, event_sink=events.append)

    def _route(*_a, **_kw):
        assert session.steer("queued then lost")
        raise RuntimeError("router down")

    with mock.patch.object(session, "_decide_route_for_turn", side_effect=_route), \
            pytest.raises(RuntimeError):
        session.run_turn("go")
    dropped = [e.payload for e in events if e.kind == "steer_dropped"]
    assert dropped == [{"turn": 1, "texts": ["queued then lost"]}]
    assert session.steer("after the raise") is False
