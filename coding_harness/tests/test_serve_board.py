"""Session board, fan-in across GUI servers, and mid-turn steering.

A real server on an ephemeral port with a gated fake model, so a test can
hold a turn open, read the board or post a steer, then let it finish.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import pytest

from coding_harness.core import session as session_mod
from coding_harness.core.replay import rebuild_messages
from coding_harness.core.session import Session
from coding_harness.modes import serve_mode, ui_mode
from coding_harness.security import sentinel
from coding_harness.tools.registry import ToolRegistry
from coding_harness.tools.write import Write

_USAGE = {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None}


class _Model:
    """Fake ollama.chat: first read of a turn calls TodoWrite, the next answers."""

    def __init__(self):
        self.calls = []
        self.gate = None  # a threading.Event held calls wait on
        self.hold = None  # hold only this 1-based call number; None holds every call
        self.entered = threading.Event()
        self.fail = None

    def __call__(self, *, model, messages, tools, on_delta=None, **_kw):
        self.calls.append([dict(m) for m in messages])
        self.entered.set()
        if self.gate is not None and self.hold in (None, len(self.calls)):
            self.gate.wait(5)
        if self.fail is not None:
            raise self.fail
        if messages[-1]["role"] == "user" and not messages[-1].get("_steer"):
            call = {"id": "c1", "function": {"name": "TodoWrite", "arguments": json.dumps(
                {"items": [{"id": "1", "text": "x", "status": "pending"}]})}}
            return {"role": "assistant", "content": "", "tool_calls": [call], "_usage": _USAGE}
        return {"role": "assistant", "content": "all done here", "tool_calls": [], "_usage": _USAGE}


def _request(method, url, body=None):
    """(status, parsed JSON) for one call."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def serve_fake():
    """Generator yielding (base url, fake model); Sentinel stubbed, MCP off."""
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
        if model.gate is not None:
            model.gate.set()
        srv.shutdown()
        srv.server_close()
        t.join(2)


@pytest.fixture()
def server():
    """A live server over the gated fake model."""
    yield from serve_fake()


def _session(base, **extra):
    """A new medium-autonomy session id."""
    status, body = _request("POST", f"{base}/v1/sessions", {"autonomy": "medium", **extra})
    assert status == 201, body
    return body["session_id"]


def _row(base, sid):
    """This session's board row."""
    status, body = _request("GET", f"{base}/v1/board")
    assert status == 200
    return next(r for r in body["sessions"] if r["session_id"] == sid)


def _wait_status(base, sid, want, timeout=5.0):
    """Poll the board until the row reaches ``want``; the last row either way."""
    row = _row(base, sid)
    for _ in range(int(timeout / 0.05)):
        if row["status"] == want:
            return row
        time.sleep(0.05)
        row = _row(base, sid)
    return row


def test_board_idle_running_done(server):
    base, model = server
    sid = _session(base)
    row = _row(base, sid)
    assert row["status"] == "idle" and row["pending"] == 0
    model.gate = threading.Event()
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/turn", {"message": "fix the widget"})
    assert status == 202
    assert model.entered.wait(5)
    row = _row(base, sid)
    assert row["status"] == "running"
    assert row["title"] == "fix the widget"
    assert row["last_excerpt"] == "fix the widget"
    model.gate.set()
    row = _wait_status(base, sid, "done")
    assert row["status"] == "done"
    assert row["last_excerpt"] == "all done here"
    assert row["last_event_ts"] and row["model"] and row["autonomy"] == "medium"


def test_board_error(server):
    base, model = server
    sid = _session(base)
    model.fail = RuntimeError("backend down")
    _request("POST", f"{base}/v1/sessions/{sid}/turn", {"message": "go"})
    assert _wait_status(base, sid, "error")["status"] == "error"


def test_board_needs_approval(server):
    base, _model = server
    sid = _session(base, interactive=True)
    entry_holder = {}
    # Park a request on the session's own broker, as a blocked tool call would.
    for obj in _live_entries():
        if obj.session.session_id == sid:
            entry_holder["e"] = obj
    broker = entry_holder["e"].broker
    t = threading.Thread(target=broker.request, args=("Bash", {"command": "ls /"}, "outside"),
                         kwargs={"timeout": 5}, daemon=True)
    t.start()
    for _ in range(100):
        if broker.list_pending():
            break
        time.sleep(0.02)
    row = _row(base, sid)
    assert row["status"] == "needs_approval" and row["pending"] == 1
    broker.deny_all()
    t.join(2)
    assert _row(base, sid)["status"] == "idle"


def _live_entries():
    """Every _SessionEntry held by a running server, found through the GC."""
    import gc
    return [o for o in gc.get_objects() if isinstance(o, serve_mode._SessionEntry)]


def _register(port, pid, name):
    """Write one GUI registry entry, as ui_mode.run does."""
    root = ui_mode.registry_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}.json"
    path.write_text(json.dumps({"pid": pid, "port": port, "cwd": f"/tmp/{name}"}))
    return path


def _dead_pid():
    """A pid that is not running."""
    for pid in range(999_999, 900_000, -1):
        try:
            os.kill(pid, 0)
        except OSError:
            return pid
    raise AssertionError("no free pid found")


def test_board_all_fans_in_and_prunes(server):
    base, _model = server
    _session(base)
    port = int(base.rsplit(":", 1)[1])
    own = _register(port, os.getpid(), "board-self")
    dead = _register(port + 1, _dead_pid(), "board-dead")
    unreachable = _register(1, os.getpid(), "board-unreachable")
    try:
        status, body = _request("GET", f"{base}/v1/board?all=1")
        assert status == 200
        assert [s["origin"] for s in body["servers"]] == [base]
        assert body["servers"][0]["sessions"]
        assert body["servers"][0]["server"]["pid"] == os.getpid()
        assert body["servers"][0]["self"] is True
        assert [e["origin"] for e in body["errors"]] == ["http://127.0.0.1:1"]
        assert not dead.exists()
    finally:
        for p in (own, unreachable):
            p.unlink(missing_ok=True)


def test_steer_idle_is_409(server):
    base, _model = server
    sid = _session(base)
    status, body = _request("POST", f"{base}/v1/sessions/{sid}/steer", {"message": "also x"})
    assert status == 409 and "/turn" in body["error"]["message"]
    status, _ = _request("POST", f"{base}/v1/sessions/{sid}/steer", {"message": ""})
    assert status == 400


def test_steer_lands_before_second_call_and_replays(server):
    base, model = server
    sid = _session(base)
    model.gate = threading.Event()
    _request("POST", f"{base}/v1/sessions/{sid}/turn", {"message": "fix it"})
    assert model.entered.wait(5)
    status, body = _request("POST", f"{base}/v1/sessions/{sid}/steer",
                            {"message": "use the small fix"})
    assert status == 202 and body["status"] == "queued"
    model.gate.set()
    assert _wait_status(base, sid, "done")["status"] == "done"
    assert len(model.calls) == 2
    first, second = model.calls
    assert not any(m.get("_steer") for m in first)
    steer = second[-1]
    assert steer == {"role": "user", "content": "use the small fix", "_steer": True}
    assert second[-2]["role"] == "tool"
    replay = rebuild_messages(session_mod.SESSIONS_DIR / f"{sid}.jsonl", "sys")
    assert replay.user_turns == 1
    assert any(m.get("_steer") and m["content"] == "use the small fix" for m in replay.messages)
    events = [e for e in _live_history(sid) if e["event"].startswith("steer_")]
    assert [e["event"] for e in events] == ["steer_queued", "steer_injected"]
    assert events[1]["step"] == 2
    assert events[0]["turn"] == events[1]["turn"] == 1


def _live_history(sid):
    """The serve-side replay history of one session."""
    for e in _live_entries():
        if e.session.session_id == sid:
            return list(e.history)
    return []


def test_wire_strips_steer_key():
    from coding_harness.models.ollama import _wire
    msgs = [{"role": "user", "content": "x", "_steer": True}]
    assert _wire(msgs) == [{"role": "user", "content": "x"}]
    assert msgs[0]["_steer"] is True


def test_rewind_skips_steer_messages(tmp_path):
    target = tmp_path / "a.txt"
    reg = ToolRegistry()
    reg.register(Write())
    reg.confirm_callback = lambda _plan: True
    session = Session(model="fake-model", registry=reg, system_prompt="be terse",
                      agentic_review=False)
    replies = [
        {"role": "assistant", "content": "", "_usage": _USAGE, "tool_calls": [
            {"id": "w1", "function": {"name": "Write", "arguments": json.dumps(
                {"file_path": str(target), "content": "hi"})}}]},
        {"role": "assistant", "content": "ok", "tool_calls": [], "_usage": _USAGE},
    ]

    def _chat(**_kw):
        if len(replies) == 2:
            session.steer("and keep it short")
        return replies.pop(0)

    allow = sentinel.SentinelVerdict(allowed=True, reason="ok", path="hook")
    with mock.patch("coding_harness.core.session.ollama.chat", side_effect=_chat), \
            mock.patch.object(sentinel, "review", return_value=allow):
        result = session.run_turn("write a")
    assert result.halted_reason == "model_done"
    steers = [m for m in session.messages if m.get("_steer")]
    assert [m["content"] for m in steers] == ["and keep it short"]
    session.rewind(1)
    assert [m["role"] for m in session.messages] == ["system"]
    assert not Path(target).exists()
