"""Parallel-sessions board and mid-turn steering routes for serve.

The board summarises every live session on this server so one GUI tab can
watch many turns. ``?all=1`` fans in the board of every registered loopback
GUI server (``ui_mode.live_servers``), one thread per origin with a short
timeout, so a stuck project never stalls the page. Steering queues an
operator message that the running turn picks up before its next model call.

Routes:
  GET  /v1/board                  -> {server:{cwd,port,pid}, sessions:[row]}
  GET  /v1/board?all=1            -> {servers:[board+origin], errors:[{origin,error}]};
                                     this server first, flagged self:true
  POST /v1/sessions/{id}/steer    {"message"} -> 202 while a turn runs, else 409

Exports: STEER_ROUTE, OPENAPI_PATHS, outcome(), session_row(), server_board(),
fan_in(), handle_board(), handle_steer().
"""
from __future__ import annotations

import http.client
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

STEER_ROUTE = "steer"
EXCERPT_MAX = 200
TITLE_MAX = 80
MAX_STEER = 20_000
FAN_IN_TIMEOUT_S = 1.0
# One shared join deadline past the socket timeout, so N slow origins cost ~1.2 s, not N x.
FAN_IN_GRACE_S = 0.2
MAX_BOARD_BYTES = 2_000_000
_LOOPBACK = re.compile(r"http://127\.0\.0\.1:\d{1,5}")

OPENAPI_PATHS: dict[str, Any] = {
    "/v1/board": {
        "get": {
            "summary": "Session board for this server; ?all=1 fans in every live GUI server",
            "responses": {"200": {"description": "board"}},
        },
    },
    "/v1/sessions/{id}/steer": {
        "post": {
            "summary": "Queue a message for the running turn's next model call: body {message}",
            "parameters": [{"name": "id", "in": "path", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {"202": {"description": "queued"},
                          "409": {"description": "no turn running; use /turn"}},
        },
    },
}


def outcome(result: Any, error: str | None) -> dict[str, Any]:
    """The finished turn's halt reason and error, for the board's done/error status."""
    return {
        "halted_reason": getattr(result, "halted_reason", None) if result is not None else "error",
        "error": error or getattr(result, "error", None),
    }


def _status(entry: Any, pending: int) -> str:
    """needs_approval, running, error, done or idle, in that precedence."""
    if pending:
        return "needs_approval"
    if entry.turn_id is not None:
        return "running"
    last = entry.last_result
    if last is None:
        return "idle"
    return "error" if last.get("error") or last.get("halted_reason") == "error" else "done"


def _excerpt(history: list[dict[str, Any]]) -> str:
    """Last assistant text in the replay history, else the last prompt, capped."""
    prompt = ""
    for payload in reversed(history):
        if payload.get("event") == "assistant_delta" and str(payload.get("text") or "").strip():
            return str(payload["text"]).strip()[-EXCERPT_MAX:]
        if not prompt and payload.get("event") == "turn_start":
            prompt = str(payload.get("prompt") or "")
    return prompt.strip()[:EXCERPT_MAX]


def session_row(entry: Any) -> dict[str, Any]:
    """One board card for a live session."""
    with entry.listeners_lock:
        history = list(entry.history)
    session = entry.session
    pending = len(entry.broker.list_pending()) if entry.broker else 0
    last = entry.last_result or {}
    return {
        "session_id": session.session_id,
        "title": (entry.title or "")[:TITLE_MAX] or None,
        "status": _status(entry, pending),
        "pending": pending,
        "last_excerpt": _excerpt(history),
        "last_event_ts": entry.last_event_ts or None,
        "turn": session._user_turn,
        "model": session.model,
        "autonomy": session.registry.autonomy.value,
        "halted_reason": last.get("halted_reason"),
    }


def server_board(state: Any, port: int) -> dict[str, Any]:
    """This server's board: identity plus one row per open session."""
    rows = []
    for sid in state.list_ids():
        entry = state.get(sid)
        if entry is not None and not entry.closed:
            rows.append(session_row(entry))
    rows.sort(key=lambda r: r["last_event_ts"] or 0, reverse=True)
    return {"server": {"cwd": os.getcwd(), "port": port, "pid": os.getpid()}, "sessions": rows}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: a board fetch only ever talks to the registered origin."""

    def redirect_request(self, *_args: Any, **_kw: Any) -> None:
        return None


def _fetch_board(origin: str) -> dict[str, Any]:
    """GET one origin's /v1/board; raises on any transport, size or parse failure."""
    # An empty ProxyHandler: http_proxy must never carry loopback prompts off the machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    with opener.open(f"{origin}/v1/board", timeout=FAN_IN_TIMEOUT_S) as resp:
        raw = resp.read(MAX_BOARD_BYTES + 1)
    if len(raw) > MAX_BOARD_BYTES:
        raise ValueError(f"board over {MAX_BOARD_BYTES} bytes")
    body = json.loads(raw)
    if not isinstance(body, dict) or not isinstance(body.get("sessions"), list):
        raise ValueError("malformed board")
    return body


def fan_in(origins: list[str]) -> dict[str, Any]:
    """Every origin's board, fetched in parallel; failures land in ``errors``."""
    boards: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []
    lock = threading.Lock()

    def _one(origin: str) -> None:
        try:
            board = {**_fetch_board(origin), "origin": origin}
            with lock:
                boards[origin] = board
        except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as e:
            with lock:
                errors.append({"origin": origin, "error": f"{type(e).__name__}: {e}"})

    threads = [threading.Thread(target=_one, args=(o,), daemon=True) for o in origins]
    for t in threads:
        t.start()
    deadline = time.monotonic() + FAN_IN_TIMEOUT_S + FAN_IN_GRACE_S
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    with lock:
        servers = [boards[o] for o in origins if o in boards]
        reported = {e["origin"] for e in errors}
        failed = errors + [{"origin": o, "error": "timeout"}
                           for o in origins if o not in boards and o not in reported]
    return {"servers": servers, "errors": failed}


def _registered_origins(self_origin: str) -> list[str]:
    """Loopback origins of live GUI servers, this one first; dead pids are pruned by the reader."""
    from coding_harness.modes import ui_mode  # lazy: ui_mode imports serve_mode

    found = sorted(set(ui_mode.live_servers().values()))
    others = [o for o in found if _LOOPBACK.fullmatch(o) and o != self_origin]
    return [self_origin, *others]


def handle_board(h: Any, state: Any, query: dict[str, list[str]]) -> None:
    """GET /v1/board, or the fan-in across servers with ``?all=1``."""
    port = int(h.server.server_port)
    if (query.get("all") or ["0"])[0] != "1":
        h._send_json(200, server_board(state, port))
        return
    self_origin = f"http://127.0.0.1:{port}"
    others = _registered_origins(self_origin)[1:]
    merged = fan_in(others)
    local = {**server_board(state, port), "origin": self_origin, "self": True}
    h._send_json(200, {"servers": [local, *merged["servers"]], "errors": merged["errors"]})


def handle_steer(h: Any, entry: Any, sid: str, body: dict[str, Any]) -> None:
    """POST /v1/sessions/{id}/steer: queue ``message`` for the running turn."""
    message = body.get("message")
    if not isinstance(message, str) or not message.strip():
        h._send_error_json(400, "body.message must be a non-empty string")
        return
    if len(message) > MAX_STEER:
        h._send_error_json(413, f"message over {MAX_STEER} chars")
        return
    # turn_id, not turn_active: /compact and revert hold turn_active with no model turn.
    with entry.listeners_lock:
        active = entry.turn_id is not None and not entry.closed
    if not active:
        h._send_error_json(409, "no turn is running; send it as a new turn with POST .../turn")
        return
    if not entry.session.steer(message.strip()):
        h._send_error_json(409, "the turn is ending; send it as a new turn with POST .../turn")
        return
    h._send_json(202, {"session_id": sid, "status": "queued"})
