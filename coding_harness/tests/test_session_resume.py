"""Session resume tests.

The load-bearing property: a resumed session never holds more authority than
the one it continues. Envelope windows are absolute instants carried across a
restart, not fresh windows started at resume time.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from coding_harness.core.envelope import envelope_from_state_dict
from coding_harness.core.mode import Mode
from coding_harness.modes import serve_mode


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _state() -> serve_mode._ServerState:
    return serve_mode._ServerState(
        model="test-model", force_local=True, explicit_model=False, enable_mcp=False,
    )


def _transcript(dirpath: Path, session_id: str, *, envelope=None, complete=True) -> Path:
    records = [
        {"kind": "session_start", "session_id": session_id, "model": "test-model",
         "mode": "act", "identity": None, "envelope": envelope},
        {"kind": "user_message", "content": "do the thing"},
        {"kind": "assistant_message", "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "Read", "arguments": "{}"}}]},
    ]
    if complete:
        records.append({"kind": "tool_result", "tool_call_id": "c1", "name": "Read",
                        "content": "file body", "is_error": False})
    records.append({"kind": "assistant_message", "content": "done", "tool_calls": []})
    p = dirpath / f"{session_id}.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return p


# ── envelope rehydration ──────────────────────────────────────────


def test_state_dict_preserves_absolute_expiry():
    expires = datetime.now(timezone.utc) + timedelta(minutes=7)
    env = envelope_from_state_dict({
        "revoked": False,
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=20)),
        "expires_at": _iso(expires),
        "grants": [{"tool": "Read", "access": "read", "path_glob": "/x/**",
                    "granted_by": "default", "expires_at": None}],
    })
    # The window must end when it originally would, not 7 minutes from now
    # plus whatever the process was down.
    assert abs((env.expires_at - expires).total_seconds()) < 1
    assert env.is_live()


def test_lapsed_envelope_stays_lapsed():
    env = envelope_from_state_dict({
        "revoked": False,
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(hours=2)),
        "expires_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
        "grants": [],
    })
    assert not env.is_live()


def test_revoked_envelope_stays_revoked():
    env = envelope_from_state_dict({
        "revoked": True, "created_at": _iso(datetime.now(timezone.utc)),
        "expires_at": None, "grants": [],
    })
    assert not env.is_live()


# ── resume ────────────────────────────────────────────────────────


def test_resume_rebuilds_messages(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_mode, "SESSIONS_DIR", tmp_path)
    _transcript(tmp_path, "sess-a")
    entry, reason = _state().resume_session("sess-a")
    assert reason == "resumed"
    assert entry.session.session_id == "sess-a"
    assert entry.session.mode is Mode.ACT
    assert [m["role"] for m in entry.session.messages] == [
        "system", "user", "assistant", "tool", "assistant"]


def test_resume_refuses_expired_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_mode, "SESSIONS_DIR", tmp_path)
    _transcript(tmp_path, "sess-b", envelope={
        "revoked": False,
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(hours=2)),
        "expires_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
        "grants": [],
    })
    entry, reason = _state().resume_session("sess-b")
    assert entry is None
    assert reason == "envelope_expired"


def test_resume_carries_live_envelope(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_mode, "SESSIONS_DIR", tmp_path)
    expires = datetime.now(timezone.utc) + timedelta(minutes=10)
    _transcript(tmp_path, "sess-c", envelope={
        "revoked": False,
        "created_at": _iso(datetime.now(timezone.utc) - timedelta(minutes=5)),
        "expires_at": _iso(expires),
        "grants": [{"tool": "Read", "access": "read", "path_glob": "/x/**",
                    "granted_by": "default", "expires_at": None}],
    })
    entry, reason = _state().resume_session("sess-c")
    assert reason == "resumed"
    assert entry.envelope is not None
    assert abs((entry.envelope.expires_at - expires).total_seconds()) < 1


def test_resume_refuses_incomplete_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_mode, "SESSIONS_DIR", tmp_path)
    _transcript(tmp_path, "sess-d", complete=False)
    entry, reason = _state().resume_session("sess-d")
    assert entry is None
    assert reason == "transcript_incomplete"


def test_resume_missing_transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_mode, "SESSIONS_DIR", tmp_path)
    entry, reason = _state().resume_session("nope")
    assert entry is None
    assert reason == "no_transcript"


def test_resume_is_idempotent_for_live_session(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_mode, "SESSIONS_DIR", tmp_path)
    _transcript(tmp_path, "sess-e")
    state = _state()
    first, _ = state.resume_session("sess-e")
    second, reason = state.resume_session("sess-e")
    assert reason == "already_live"
    assert second is first


def test_restore_refuses_on_started_session(tmp_path, monkeypatch):
    monkeypatch.setattr(serve_mode, "SESSIONS_DIR", tmp_path)
    _transcript(tmp_path, "sess-f")
    entry, _ = _state().resume_session("sess-f")
    try:
        entry.session.restore([], user_turns=0)
    except RuntimeError:
        return
    raise AssertionError("restore() must refuse on an already-started session")
