"""Headless HTTP serve mode for the coding harness.

Exposes the harness as a small HTTP API so APScheduler pipelines and other
in-process automation stop subprocessing ``claude --print``. The
surface intentionally maps 1:1 onto ``Session`` so there's no second agent
loop to keep in sync.

Endpoints (versioned; ``/v1`` prefix):

  GET  /v1/healthz              process liveness + active session count
  GET  /v1/openapi.json         OpenAPI 3.x spec for the surface
  POST /v1/sessions             create session: body {"mode":"plan|act",
                                 "autonomy":"off|low|medium|high"}
  POST /v1/sessions/{id}/turn   run one prompt — body {"message":"..."};
                                 202 + turn_id, output on the event stream,
                                 or {"wait":true} to block for the result
  GET  /v1/sessions/{id}/audit  session audit entries (filtered by id)
  GET  /v1/sessions/{id}/events SSE stream of session events (JSON-RPC
                                 notifications inside ``data:`` lines)
  .../plan, .../act, .../autonomy, .../todo   spec-then-run; see serve_plan
  .../memories, .../pin, /v1/skills/suggest     recall and memory; see serve_brain_extra
  GET /v1/board, POST .../steer  session board and mid-turn steering; see serve_board
  /v1/git, /v1/git/commit, /v1/worktrees[/open]   git panel; see serve_git
  GET  /v1/transcripts?q=, POST /v1/transcripts/{id}/{title,delete}
                                 history; see serve_history
  GET, PUT /v1/settings         the user settings file; see serve_settings

Event streaming uses Server-Sent Events instead of WebSocket. The bead
text mentions WebSocket but its own NOTES line pins the implementation
to stdlib ``http.server``, which has no WebSocket support. SSE is
HTTP-native, supports the one-way server→client streaming pattern that
the harness already produces, and lets a curl client subscribe with no
extra dependency. JSON-RPC 2.0 notifications still ride inside each
event's ``data:`` block, so a client that wanted to upgrade to WS later
keeps the same payload contract.

Default mode is **Plan** per AC #6 — the same fail-closed posture that
guards the harness everywhere else. Mode follows the autonomy level, which
POST .../autonomy (or .../plan and .../act) may change mid-session. The autonomy level defaults to
LOW; when the body names one, the mode follows it (off is Plan, the rest
Act). Settings come from the server's cwd once at start.
"""
from __future__ import annotations

import json
import mimetypes
import os
import queue
import socketserver
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

from coding_harness.core import transcripts
from coding_harness.core.envelope import (
    EnvelopeSpecError,
    SessionEnvelope,
    envelope_from_dict,
    envelope_from_state_dict,
    grant_for,
    preset,
)
from coding_harness.core.hooks import HookRunner
from coding_harness.core.hooks import build_runner as build_hook_runner
from coding_harness.core.mode import Autonomy, Mode, mode_for, parse_autonomy
from coding_harness.core.permissions import PermissionBroker
from coding_harness.core.replay import rebuild_messages
from coding_harness.core.session import SESSIONS_DIR, Session
from coding_harness.core.settings import Settings, load_settings, resolve_autonomy
from coding_harness.modes import (
    ops,
    serve_auth,
    serve_board,
    serve_brain_extra,
    serve_changes,
    serve_composer,
    serve_git,
    serve_gui,
    serve_history,
    serve_origin,
    serve_plan,
    serve_settings,
)
from coding_harness.modes.print_mode import build_registry, build_system_prompt
from coding_harness.security import audit
from coding_harness.tools.registry import DispatchEvent

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9100
SSE_MAX_QUEUE = 256
# Events kept per session so a reloaded tab rebuilds its thread. Streamed
# deltas are coalesced on append, so this bounds tool steps, not tokens.
SSE_HISTORY_MAX = 4000
SSE_KEEPALIVE_SECONDS = 15.0
SHUTDOWN_GRACE = 1.0
# Browser origin allowed to call this API (the Vite dev server for the P2 UI).
# The server still binds loopback by default, so CORS here widens which local
# pages may script against it, not who can reach it.
DEFAULT_UI_ORIGIN = "http://localhost:5173"

# The built SPA. Serving it from here is what makes `bjorn serve` plus one
# browser tab a working session: no second dev server, no CORS, one origin.
# Absent on a fresh clone (ui/dist is gitignored), so a missing build answers
# with instructions rather than a bare 404.
UI_DIST = Path(__file__).resolve().parents[1] / "ui" / "dist"
UI_INDEX = "index.html"
# Vite content-hashes everything under assets/, so those are immutable; the
# entry document must never be cached or a deploy is invisible until a reload.
_IMMUTABLE_PREFIX = "assets/"
_EXTRA_TYPES = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".map": "application/json; charset=utf-8",
    ".webmanifest": "application/manifest+json",
    ".svg": "image/svg+xml",
}


def _ui_asset(url_path: str) -> Path | None:
    """The file under ``UI_DIST`` a URL names, or None if it escapes the root.

    Unquotes first so percent-encoded traversal (``%2e%2e%2f``) is caught by
    the same containment check as the literal form.
    """
    relative = unquote(url_path).lstrip("/") or UI_INDEX
    candidate = (UI_DIST / relative).resolve()
    root = UI_DIST.resolve()
    if candidate != root and root not in candidate.parents:
        return None
    return candidate

AGENTS_YAML = Path(os.path.expanduser(os.environ.get("HARNESS_AGENTS_FILE", "~/.config/bjorn/agents.yaml")))


def _resolve_identity(name: str) -> dict[str, str | None]:
    """Resolve an agent name to a DID-style identity + key prefix.

    Best-effort against ``AGENTS_YAML`` (read-only, may be
    absent in tests). On any friction the identity degrades to
    ``carryall:{name}`` with no key — the stamp is still real provenance,
    just without the pubkey discriminator. Per-action Ed25519 signing is P3.
    """
    ident: dict[str, str | None] = {
        "identity": f"carryall:{name}",
        "identity_key": None,
    }
    try:
        import yaml
        data = yaml.safe_load(AGENTS_YAML.read_text(encoding="utf-8")) or {}
        agents = data.get("agents") if isinstance(data.get("agents"), dict) else data
        rec = agents.get(name) if isinstance(agents, dict) else None
        pub = rec.get("public_key") if isinstance(rec, dict) else None
        if pub:
            ident["identity"] = f"carryall:{name}#{str(pub)[:8]}"
            ident["identity_key"] = str(pub)[:16]
    except Exception:  # noqa: BLE001 — identity is best-effort provenance
        pass
    return ident


@dataclass
class _SessionEntry:
    """One server-side session: the agent + its event fanout queues.

    ``event_sink`` on the Session writes into every queue in ``listeners``
    so multiple SSE subscribers can fan out from one source. Per-listener
    queues are bounded; on overflow we drop the oldest event for that
    listener, never the producer.
    """

    session: Session
    lock: threading.Lock = field(default_factory=threading.Lock)
    listeners: list[queue.Queue[dict[str, Any]]] = field(default_factory=list)
    listeners_lock: threading.Lock = field(default_factory=threading.Lock)
    closed: bool = False
    # P1 governance: every session carries an envelope (explicit spec or the
    # cwd preset); the broker exists only for sessions created interactive.
    envelope: SessionEnvelope | None = None
    broker: PermissionBroker | None = None
    identity: str | None = None
    # Settings-declared lifecycle hooks; None when none are configured.
    hooks: HookRunner | None = None
    # Set while a background turn holds (or is about to hold) ``lock``. Guards
    # the accept decision so queued POSTs cannot pile blocked worker threads up
    # behind a long turn; a second turn is refused, not silently enqueued.
    turn_active: bool = False
    turn_id: str | None = None
    # Replayed to every new listener (under listeners_lock) before live events.
    history: deque[dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=SSE_HISTORY_MAX))
    title: str | None = None
    # True when the envelope is the cwd preset, so an autonomy change may
    # rebuild its default grants (serve_plan.apply_autonomy). A custom spec
    # or a resumed envelope keeps the scope it was given.
    envelope_preset: bool = False
    # Park every Write/Edit on the broker as a kind:"review" request, set by
    # POST .../review; "Apply all" (allow_always) clears it.
    review_writes: bool = False
    # The last finished turn's {halted_reason, error} and the wall time of the
    # latest event, for the board's done/error status and elapsed column.
    last_result: dict[str, Any] | None = None
    last_event_ts: float = 0.0


def _remember(history: deque[dict[str, Any]], payload: dict[str, Any]) -> None:
    """Append to the replay history, folding consecutive deltas into one."""
    last = history[-1] if history else None
    if (last is not None and payload["event"] == "assistant_delta"
            and last["event"] == "assistant_delta"):
        history[-1] = {**last, "text": str(last.get("text", "")) + str(payload.get("text", ""))}
        return
    history.append(payload)


class _ServerState:
    def __init__(
        self,
        *,
        model: str,
        force_local: bool,
        explicit_model: bool,
        enable_mcp: bool,
        ui_origin: str = DEFAULT_UI_ORIGIN,
        frontier_backend: str = "api",
        settings: Settings | None = None,
        brain: bool = False,
    ):
        # The second brain reaches only sessions a person is watching (the
        # GUI launcher). The fleet's serve and bead workers never get it:
        # what they write ends up in PR bodies that leave the machine.
        self.brain = brain
        self.model = model
        self.force_local = force_local
        self.explicit_model = explicit_model
        self.enable_mcp = enable_mcp
        self.ui_origin = ui_origin
        self.frontier_backend = frontier_backend
        self.settings = settings if settings is not None else load_settings(os.getcwd())
        self._sessions: dict[str, _SessionEntry] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        *,
        mode: Mode,
        identity: str | None = None,
        envelope_spec: dict[str, Any] | None = None,
        session_id: str | None = None,
        envelope_obj: SessionEnvelope | None = None,
        interactive: bool = False,
        autonomy: Autonomy | None = None,
        resumed: bool = False,
    ) -> _SessionEntry:
        # An explicit level decides the mode; without one the caller's mode
        # stands (Plan by default) and the level is LOW, so an Act session
        # runs the LOW command set.
        level = Autonomy.LOW if autonomy is None else autonomy
        if autonomy is not None:
            mode = mode_for(level)
        registry = build_registry(
            event_sink=None, enable_mcp=self.enable_mcp,
            brain_block=self.settings.brain if self.brain else None,
            # Same audience rule as the brain: the GUI launcher, not the fleet.
            subagents=self.brain, settings=self.settings,
        )
        registry.autonomy = level
        registry.settings = self.settings

        entry = _SessionEntry(session=None)  # type: ignore[arg-type]
        # Auto-accept writes unless the operator switched on review: often no
        # one is at the other end of an HTTP call. Plan mode (the default)
        # keeps act-only tools out of the model's reach entirely; sessions
        # that opt into Act explicitly take responsibility for what they invoke.
        registry.confirm_callback = lambda plan: serve_changes.review_gate(entry, plan)

        def _fanout(event: DispatchEvent) -> None:
            payload = {"event": event.kind, **event.payload}
            with entry.listeners_lock:
                _remember(entry.history, payload)
                entry.last_event_ts = time.time()
                listeners = list(entry.listeners)
            for q in listeners:
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    # Drop the oldest event for this slow listener and try once
                    # more. If still full, give up — the producer keeps moving.
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except (queue.Empty, queue.Full):
                        pass

        # envelope_obj wins: a resume passes an already-rehydrated envelope
        # whose absolute expiries must not be restarted by envelope_from_dict.
        if envelope_obj is not None:
            envelope = envelope_obj
        elif envelope_spec:
            envelope = envelope_from_dict(envelope_spec)
        else:
            envelope = preset(os.getcwd(), level, self.settings)
        ident = _resolve_identity(identity) if identity else {"identity": None}

        session_kwargs: dict[str, Any] = {}
        if session_id is not None:
            session_kwargs["session_id"] = session_id
        session = Session(
            model=self.model,
            registry=registry,
            system_prompt=build_system_prompt(),
            event_sink=_fanout,
            force_local=self.force_local,
            explicit_model=self.explicit_model,
            mode=mode,
            envelope=envelope,
            agent_identity=ident["identity"],
            frontier_backend=self.frontier_backend,
            shadow_checkpoints=True,
            **session_kwargs,
        )
        # Proposals wait for an operator decision and never leave the machine,
        # so plain serve drafts them too once the user configured a brain.
        session.propose_memories = self.brain or bool(self.settings.brain)
        session.recall_hints = True
        entry.session = session
        if registry.brain_error:
            session._emit("brain_unavailable", {"reason": registry.brain_error})
        # Print mode and the REPL attach the hook runner; serve and the GUI
        # must too, or the default `bjorn` runs every tool call ungated.
        entry.hooks = build_hook_runner(
            self.settings,
            session_id=getattr(session, "session_id", "unknown"),
            cwd=os.getcwd(),
        )
        registry.hooks = entry.hooks
        if entry.hooks is not None and not resumed:
            entry.hooks.run("SessionStart")
        entry.envelope = envelope
        # A resumed session keeps the scope it resumed with, even when its
        # transcript carried no envelope and the preset filled in.
        entry.envelope_preset = envelope_obj is None and not envelope_spec and not resumed
        entry.identity = ident["identity"]

        # A broker parks out-of-envelope calls until an operator decides, so
        # only an interactive session gets one; a headless session hard-denies
        # at once instead of stalling a turn for the broker timeout. The
        # allow_always path widens the envelope and records the grant on the
        # audit chain before the blocked turn resumes.
        if interactive:
            def _broker_emit(kind: str, payload: dict[str, Any]) -> None:
                payload = serve_changes.annotate_resolved(entry, kind, payload)
                _fanout(DispatchEvent(kind=kind, payload=payload))

            def _on_grant(req: Any) -> None:
                grant = grant_for(req.tool, req.args, expiry_minutes=req.expiry_minutes)
                # Fail closed: record the widening on the tamper-
                # evident chain BEFORE it takes effect. If the audit append
                # raises (full disk, corrupt chain tail), the exception
                # propagates out of on_grant → broker.request → dispatch errors,
                # and the envelope is NOT widened. A governance-expanding event
                # that can't be audited must not happen silently.
                audit.append_envelope_change(
                    session_id=session.session_id,
                    action="grant",
                    grant=grant.to_dict(),
                    reason=req.reason,
                    agent_identity=entry.identity,
                )
                envelope.add_grant(grant)

            broker = PermissionBroker(emit=_broker_emit, on_grant=_on_grant)
            registry.permission_broker = broker
            entry.broker = broker

        with self._lock:
            self._sessions[session.session_id] = entry
        return entry

    def get(self, session_id: str) -> _SessionEntry | None:
        with self._lock:
            return self._sessions.get(session_id)

    def resume_session(
        self, session_id: str, *, interactive: bool = False,
    ) -> tuple[_SessionEntry | None, str]:
        """Rebuild a session from its on-disk transcript. Returns (entry, reason).

        Refuses (entry None) when the transcript is absent, predates
        tool-result logging, or carries an envelope that has since lapsed — a
        resumed session must never hold more authority than the one it
        continues. Live sessions are returned as-is, so this is idempotent.
        """
        existing = self.get(session_id)
        if existing is not None:
            return existing, "already_live"

        path = SESSIONS_DIR / f"{session_id}.jsonl"
        if not path.exists():
            return None, "no_transcript"
        # Tools resolve against this process's cwd; a session from another
        # project would silently act on the wrong tree.
        if transcripts.transcript_cwd(path) not in (None, os.getcwd()):
            return None, "other_project"

        replayed = rebuild_messages(path, build_system_prompt())
        if not replayed.complete:
            return None, "transcript_incomplete"

        envelope = None
        if replayed.envelope is not None:
            envelope = envelope_from_state_dict(replayed.envelope)
            if not envelope.is_live():
                return None, "envelope_expired"

        entry = self.create_session(
            mode=Mode(replayed.mode) if replayed.mode else Mode.PLAN,
            identity=replayed.agent_identity,
            session_id=session_id,
            envelope_obj=envelope,
            interactive=interactive,
            resumed=True,
        )
        # The old thread goes in first, so the restore's own events follow it.
        with entry.listeners_lock:
            for payload in serve_gui.transcript_events(path):
                _remember(entry.history, payload)
        entry.session.restore(replayed.messages, user_turns=replayed.user_turns,
                              sensitive=replayed.sensitive)
        prompt = transcripts.first_prompt(path)
        if prompt:
            entry.title = serve_gui.skill_title(prompt) or prompt.strip().splitlines()[0][:80]
        try:
            audit.append_session_resume(
                session_id=session_id,
                messages=len(replayed.messages),
                user_turns=replayed.user_turns,
                agent_identity=entry.identity,
            )
        except Exception as e:  # noqa: BLE001 — telemetry must not block resume
            entry.session._log("resume_audit_error", {"error": f"{type(e).__name__}: {e}"})
        return entry, "resumed"

    def list_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions.keys())

    def list_sessions(self) -> list[dict[str, Any]]:
        """Summaries for GET /v1/sessions — newest first."""
        with self._lock:
            entries = list(self._sessions.values())
        out = []
        for e in entries:
            out.append({
                "session_id": e.session.session_id,
                "mode": e.session.mode.value,
                "autonomy": e.session.registry.autonomy.value,
                "model": e.session.model,
                "explicit_model": e.session.explicit_model,
                "kind": e.session.kind,
                "identity": e.identity,
                "revoked": e.envelope.revoked if e.envelope else False,
                "has_envelope": e.envelope is not None,
                "closed": e.closed,
                "title": e.title,
                "turn_active": e.turn_active,
                "review_writes": e.review_writes,
            })
        out.sort(key=lambda s: s["session_id"], reverse=True)
        return out

    def close(self) -> None:
        with self._lock:
            entries = list(self._sessions.values())
            self._sessions.clear()
        for entry in entries:
            try:
                entry.session.close()
            except Exception:  # noqa: BLE001
                pass
            entry.closed = True

    def attach_listener(self, entry: _SessionEntry) -> queue.Queue[dict[str, Any]]:
        """A listener queue pre-filled with the session's history.

        Filled and registered under one lock hold, so no event lands between
        the replay and the first live event.
        """
        with entry.listeners_lock:
            q: queue.Queue[dict[str, Any]] = queue.Queue(
                maxsize=SSE_MAX_QUEUE + len(entry.history))
            for payload in entry.history:
                q.put_nowait(payload)
            entry.listeners.append(q)
        return q

    def detach_listener(
        self, entry: _SessionEntry, q: queue.Queue[dict[str, Any]]
    ) -> None:
        with entry.listeners_lock:
            try:
                entry.listeners.remove(q)
            except ValueError:
                pass


# ── HTTP handler ─────────────────────────────────────────────────────


def _make_handler(state: _ServerState) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        # Quieter than the default per-request stderr line.
        def log_message(self, format: str, *args: Any) -> None:
            line = format % args
            sys.stderr.write(f"[serve] {self.address_string()} - {line}\n")

        def parse_request(self) -> bool:
            """Parse the request line, then run the auth gate before any verb.

            One hook for every method and path, so routes added later are
            gated without registering them anywhere (see serve_auth).
            """
            if not super().parse_request():
                return False
            refused = serve_origin.refusal(self, state.ui_origin, getattr(state, "auth", None))
            if refused is not None:
                self.close_connection = True
                self._send_error_json(*refused)
                return False
            gate = getattr(state, "auth", None)
            return gate is None or not gate.intercept(self)

        # ── routing helpers ──────────────────────────────────────

        def _send_cors_headers(self) -> None:
            self.send_header("Access-Control-Allow-Origin", state.ui_origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

        def _send_json(
            self, status: int, payload: Any,
            *, extra_headers: dict[str, str] | None = None,
        ) -> None:
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._send_cors_headers()
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_ui(self, url_path: str) -> None:
            """Serve the built SPA, falling back to index.html for client routes."""
            if not UI_DIST.is_dir():
                self._send_error_json(
                    503,
                    "UI not built: run `make ui` (or `npm run build` in "
                    f"coding_harness/ui). Expected {UI_DIST}",
                )
                return
            target = _ui_asset(url_path)
            if target is None:
                self._send_error_json(403, "forbidden")
                return
            if not target.is_file():
                if target.suffix:
                    # A path naming a file type is an asset, not a route. Serving
                    # the shell for it returns HTML under an image or script URL,
                    # so a bad build looks like a rendering bug instead of a 404.
                    self._send_error_json(404, f"not found: {url_path}")
                    return
                # Extensionless and unmatched is a client-side route; hand back
                # the shell and let the router resolve it.
                target = UI_DIST / UI_INDEX
                if not target.is_file():
                    self._send_error_json(503, f"UI build is missing {UI_INDEX}")
                    return
            try:
                body = target.read_bytes()
            except OSError as e:
                self._send_error_json(500, f"cannot read UI asset: {e}")
                return
            suffix = target.suffix.lower()
            ctype = _EXTRA_TYPES.get(suffix) or mimetypes.guess_type(target.name)[0] \
                or "application/octet-stream"
            relative = target.resolve().relative_to(UI_DIST.resolve()).as_posix()
            cache = ("public, max-age=31536000, immutable"
                     if relative.startswith(_IMMUTABLE_PREFIX) else "no-cache")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache)
            self.end_headers()
            self.wfile.write(body)

        def _send_error_json(self, status: int, message: str) -> None:
            self._send_json(status, {"error": {"code": status, "message": message}})

        def _read_json_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))

        # ── HTTP verbs ───────────────────────────────────────────

        def do_OPTIONS(self) -> None:  # noqa: N802 — stdlib name
            # CORS preflight for the browser UI. No body, no routing.
            self.send_response(204)
            self._send_cors_headers()
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802 — stdlib name
            path = self.path.split("?", 1)[0]
            if path == "/v1/sessions":
                self._send_json(200, {"sessions": state.list_sessions()})
                return
            if path == "/v1/routines":
                self._send_json(200, ops.list_routines())
                return
            if path == "/v1/models":
                self._send_json(200, ops.list_models())
                return
            if path == "/v1/models/roles":
                from coding_harness.core import model_roles
                self._send_json(200, {"roles": model_roles.table()})
                return
            if path == "/v1/ollama":
                self._send_json(200, ops.ollama_status())
                return
            if path == "/v1/skills":
                self._send_json(200, serve_gui.list_skills(os.getcwd()))
                return
            if path == "/v1/skills/suggest":
                serve_brain_extra.suggest(self, (parse_qs(self.path.partition("?")[2]).get("q") or [""])[0])
                return
            if path.startswith("/v1/skills/"):
                detail = serve_gui.skill_detail(unquote(path[len("/v1/skills/"):]), os.getcwd())
                if detail is None:
                    self._send_error_json(404, "no such skill")
                    return
                self._send_json(200, detail)
                return
            if path.startswith("/v1/brain/"):
                self._handle_brain_get(path, parse_qs(self.path.partition("?")[2]))
                return
            if path == "/v1/files":
                q = (parse_qs(self.path.partition("?")[2]).get("q") or [""])[0]
                self._send_json(200, serve_composer.list_files(os.getcwd(), q))
                return
            if path == "/v1/commands":
                self._send_json(200, serve_composer.list_commands(os.getcwd()))
                return
            if path == "/v1/projects":
                self._send_json(200, serve_gui.list_projects(os.getcwd()))
                return
            if path == "/v1/transcripts":
                q = (parse_qs(self.path.partition("?")[2]).get("q") or [""])[0]
                self._send_json(200, serve_history.list_history(
                    os.getcwd(), set(state.list_ids()), q))
                return
            if path == "/v1/settings":
                self._send_json(*serve_settings.get_settings(os.getcwd()))
                return
            if path == "/v1/work":
                query = parse_qs(self.path.partition("?")[2])
                status_q = (query.get("status") or ["open"])[0]
                self._send_json(200, ops.list_work(status_q))
                return
            if path == "/v1/healthz":
                self._send_json(200, {
                    "status": "ok",
                    "cwd": os.getcwd(),
                    "default_model": state.model,
                    "default_autonomy": resolve_autonomy(None, state.settings).value,
                    "active_sessions": len(state.list_ids()),
                    "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                })
                return
            if path in serve_git.GET_PATHS:
                serve_git.handle(self, state, "GET", path, parse_qs(self.path.partition("?")[2]))
                return
            if path == "/v1/openapi.json":
                self._send_json(200, _OPENAPI_SPEC)
                return
            if path == "/v1/board":
                serve_board.handle_board(self, state, parse_qs(self.path.partition("?")[2]))
                return
            if path.startswith("/v1/sessions/"):
                tail = path[len("/v1/sessions/"):]
                if "/" not in tail:
                    self._send_error_json(404, f"not found: {path}")
                    return
                session_id, sub = tail.split("/", 1)
                entry = state.get(session_id)
                if entry is None:
                    self._send_error_json(404, f"session not found: {session_id}")
                    return
                if sub == "audit":
                    self._handle_audit(session_id)
                    return
                if sub == "events":
                    self._handle_events(entry)
                    return
                if sub == "permissions":
                    self._handle_list_permissions(entry)
                    return
                if sub == "envelope":
                    self._handle_get_envelope(entry)
                    return
                if sub in serve_plan.GET_ROUTES:
                    serve_plan.handle(self, state, "GET", entry, session_id, sub)
                    return
                if sub == "diff":
                    self._send_json(*serve_changes.get_diff(
                        entry, parse_qs(self.path.partition("?")[2])))
                    return
                if sub in serve_brain_extra.GET_ROUTES:
                    serve_brain_extra.handle(self, state, "GET", entry, session_id, sub)
                    return
            if not path.startswith("/v1/") and path != "/v1":
                # Anything that is not the API is the app. Checked last so a
                # mistyped /v1 route still answers JSON rather than the shell.
                self._send_ui(path)
                return
            self._send_error_json(404, f"not found: {path}")

        def do_POST(self) -> None:  # noqa: N802 — stdlib name
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_error_json(400, "invalid JSON body")
                return
            # Every POST body must be a JSON object. A bare array/scalar
            # (``[1]``, ``"x"``, ``3``) parses fine but then AttributeErrors on
            # body.get() in the handler thread, dropping the connection with no
            # status. Reject it here as a 400.
            if not isinstance(body, dict):
                self._send_error_json(400, "request body must be a JSON object")
                return

            if path == "/v1/sessions":
                self._handle_create_session(body)
                return
            if path.startswith("/v1/sessions/") and path.endswith("/turn"):
                session_id = path[len("/v1/sessions/"):-len("/turn")]
                self._handle_turn(session_id, body)
                return
            if path.startswith("/v1/sessions/") and path.endswith("/resume"):
                session_id = path[len("/v1/sessions/"):-len("/resume")]
                self._handle_resume(session_id, body)
                return
            if path.startswith("/v1/transcripts/"):
                self._send_json(*serve_history.post_transcript(
                    path[len("/v1/transcripts/"):], body, set(state.list_ids()),
                    os.getcwd()))
                return
            if path.startswith("/v1/skills/"):
                self._handle_save_skill(unquote(path[len("/v1/skills/"):]), body)
                return
            if path == "/v1/models/probe":
                self._handle_probe(body)
                return
            if path == "/v1/projects/open":
                self._handle_open_project(body)
                return
            if path in serve_git.POST_PATHS:
                serve_git.handle(self, state, "POST", path,
                                 parse_qs(self.path.partition("?")[2]), body)
                return
            if path.startswith("/v1/sessions/") and path.endswith("/compact"):
                session_id = path[len("/v1/sessions/"):-len("/compact")]
                self._handle_compact(session_id)
                return
            if path.startswith("/v1/sessions/") and path.endswith("/kind"):
                self._handle_set_kind(path[len("/v1/sessions/"):-len("/kind")], body)
                return
            if path.startswith("/v1/sessions/") and path.endswith("/model"):
                session_id = path[len("/v1/sessions/"):-len("/model")]
                self._handle_set_model(session_id, body)
                return
            if path.startswith("/v1/sessions/") and path.endswith("/interrupt"):
                session_id = path[len("/v1/sessions/"):-len("/interrupt")]
                self._handle_interrupt(session_id)
                return
            if path.startswith("/v1/sessions/") and path.endswith("/revoke"):
                session_id = path[len("/v1/sessions/"):-len("/revoke")]
                self._handle_revoke(session_id, body)
                return
            if self._plan_route("POST", path, body):
                return
            if path.startswith("/v1/sessions/") and path.endswith("/" + serve_board.STEER_ROUTE):
                session_id = path[len("/v1/sessions/"):-len("/" + serve_board.STEER_ROUTE)]
                entry = state.get(session_id)
                if entry is None:
                    self._send_error_json(404, f"session not found: {session_id}")
                    return
                serve_board.handle_steer(self, entry, session_id, body)
                return
            # POST /v1/sessions/{id}/permissions/{req_id}
            if path.startswith("/v1/sessions/") and "/permissions/" in path:
                tail = path[len("/v1/sessions/"):]
                session_id, _, req_id = tail.partition("/permissions/")
                self._handle_resolve_permission(session_id, req_id, body)
                return
            if path.startswith("/v1/sessions/"):
                session_id, _, sub = path[len("/v1/sessions/"):].partition("/")
                if sub in serve_changes.POST_ROUTES:
                    entry = state.get(session_id)
                    if entry is None:
                        self._send_error_json(404, f"session not found: {session_id}")
                        return
                    self._send_json(*serve_changes.POST_ROUTES[sub](entry, body))
                    return
            self._send_error_json(404, f"not found: {path}")

        def do_PUT(self) -> None:  # noqa: N802 — stdlib name
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json_body()
            except json.JSONDecodeError:
                self._send_error_json(400, "invalid JSON body")
                return
            if not isinstance(body, dict):
                self._send_error_json(400, "request body must be a JSON object")
                return
            if path == "/v1/settings":
                self._send_json(*serve_settings.put_settings(
                    body, os.getcwd(), self._adopt_settings))
                return
            if not self._plan_route("PUT", path, body):
                self._send_error_json(404, f"not found: {path}")

        def _plan_route(self, method: str, path: str, body: dict[str, Any]) -> bool:
            """Hand /v1/sessions/{id}/{plan,act,autonomy} to serve_plan."""
            routes = serve_plan.POST_ROUTES if method == "POST" else serve_plan.PUT_ROUTES
            extra = serve_brain_extra.POST_ROUTES if method == "POST" else frozenset()
            if not path.startswith("/v1/sessions/"):
                return False
            session_id, _, sub = path[len("/v1/sessions/"):].partition("/")
            if sub not in routes and sub not in extra:
                return False
            entry = state.get(session_id)
            if entry is None:
                self._send_error_json(404, f"session not found: {session_id}")
                return True
            handler = serve_plan if sub in routes else serve_brain_extra
            handler.handle(self, state, method, entry, session_id, sub, body)
            return True

        # ── handlers ─────────────────────────────────────────────

        def _handle_create_session(self, body: dict[str, Any]) -> None:
            mode_raw = (body.get("mode") or "plan").lower()
            try:
                mode = Mode(mode_raw)
            except ValueError:
                self._send_error_json(
                    400, f"invalid mode '{mode_raw}'; expected 'plan' or 'act'"
                )
                return
            identity = body.get("identity")
            if identity is not None and not isinstance(identity, str):
                self._send_error_json(400, "body.identity must be a string")
                return
            envelope_spec = body.get("envelope")
            if envelope_spec is not None and not isinstance(envelope_spec, dict):
                self._send_error_json(400, "body.envelope must be an object")
                return
            interactive = body.get("interactive", False)
            if not isinstance(interactive, bool):
                self._send_error_json(400, "body.interactive must be a boolean")
                return
            autonomy, problem = _autonomy_from_body(body)
            if problem is not None:
                self._send_error_json(400, problem)
                return
            kind = body.get("kind", "code")
            if kind not in ("code", "chat"):
                self._send_error_json(400, "body.kind must be 'code' or 'chat'")
                return
            try:
                entry = state.create_session(
                    mode=mode, identity=identity, envelope_spec=envelope_spec,
                    interactive=interactive, autonomy=autonomy,
                )
            except EnvelopeSpecError as e:
                self._send_error_json(400, f"invalid envelope: {e}")
                return
            session = entry.session
            if kind == "chat":
                session.set_kind("chat")
            self._send_json(201, {
                "session_id": session.session_id,
                "mode": session.mode.value,
                "autonomy": session.registry.autonomy.value,
                "model": session.model,
                "kind": session.kind,
                "identity": entry.identity,
                "envelope": entry.envelope.to_dict() if entry.envelope else None,
            })

        def _handle_turn(self, session_id: str, body: dict[str, Any]) -> None:
            entry = state.get(session_id)
            if entry is None:
                self._send_error_json(404, f"session not found: {session_id}")
                return
            message = body.get("message")
            typed = message if isinstance(message, str) else ""
            if typed and body.get("skill") is None:
                message = serve_composer.expand_custom(typed, os.getcwd())
            skill = body.get("skill")
            if skill is not None:
                if not isinstance(skill, str) or not skill:
                    self._send_error_json(400, "body.skill must be a non-empty string")
                    return
                if message is not None and not isinstance(message, str):
                    self._send_error_json(400, "body.message must be a string")
                    return
                message = serve_gui.skill_prompt(skill, message or "", os.getcwd())
                if message is None:
                    self._send_error_json(404, f"no such skill: {skill}")
                    return
            attach = body.get("attach")
            if attach is not None:
                if not _string_list(attach) or not attach:
                    self._send_error_json(400, "body.attach must be a non-empty list of note paths")
                    return
                client = self._brain()
                if client is None:
                    self._send_error_json(404, "no second brain configured")
                    return
                from coding_harness.context.brain import BrainError
                try:
                    message = serve_gui.attach_notes(client, attach, entry.session,
                                                     message if isinstance(message, str) else "")
                except BrainError as e:
                    self._send_error_json(502, f"could not attach: {e}")
                    return
            if not isinstance(message, str) or not message:
                self._send_error_json(400, "body.message must be a non-empty string")
                return
            mentioned = serve_composer.mention_line(typed, os.getcwd())
            if mentioned:
                message = f"{message}\n\n{mentioned}"
            model_override = body.get("model")
            if model_override is not None:
                if not isinstance(model_override, str) or not model_override:
                    self._send_error_json(400, "body.model must be a non-empty string")
                    return
                if model_override != "claude-cli":
                    from coding_harness.models.ollama import (
                        BannedModelError,
                        assert_model_allowed,
                    )
                    try:
                        assert_model_allowed(model_override)
                    except BannedModelError as e:
                        self._send_error_json(400, str(e))
                        return
            max_time_s = body.get("max_time_s")
            if max_time_s is not None and not _positive_number(max_time_s):
                self._send_error_json(400, "body.max_time_s must be a positive number")
                return
            checks = body.get("checks")
            if checks is not None and not _string_list(checks):
                self._send_error_json(400, "body.checks must be a list of strings")
                return
            wait = body.get("wait", False)
            if not isinstance(wait, bool):
                self._send_error_json(400, "body.wait must be a boolean")
                return

            if entry.title is None:
                entry.title = (serve_gui.skill_title(message)
                               or message.strip().splitlines()[0][:80])

            if not wait:
                self._start_turn(
                    entry, session_id, message,
                    model_override=model_override, max_time_s=max_time_s,
                    checks=checks,
                )
                return

            # Serialize turns within a session — tool dispatch and audit
            # writes assume sequential execution. Different sessions still
            # run in parallel. The claim mirrors _start_turn so the board
            # and /steer see a blocking turn as running.
            with entry.listeners_lock:
                if entry.turn_active:
                    self._send_error_json(
                        409, f"a turn is already running (turn {entry.turn_id})"
                    )
                    return
                entry.turn_active = True
                entry.turn_id = uuid.uuid4().hex[:12]
            result, error = None, None
            try:
                with entry.lock:
                    if entry.closed:
                        self._send_error_json(410, "session closed")
                        return
                    entry.session.done_checks = list(checks or [])
                    result = _hooked_turn(entry, message, model_override, max_time_s)
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                raise
            finally:
                with entry.listeners_lock:
                    if result is not None or error is not None:
                        entry.last_result = serve_board.outcome(result, error)
                    entry.turn_active = False
                    entry.turn_id = None
            self._send_json(200, {
                "session_id": session_id,
                "text": result.final_text,
                "turns": result.turns,
                "halted_reason": result.halted_reason,
                "error": result.error,
                "tokens_in": result.tokens_in,
                "tokens_out": result.tokens_out,
                "files_changed": result.files_changed,
                "checks_passed": result.checks_passed,
                "checks_report": result.checks_report,
            })

        def _start_turn(
            self, entry: _SessionEntry, session_id: str, message: str,
            *, model_override: str | None, max_time_s: float | None,
            checks: list[str] | None,
            scope: Callable[[], Any] | None = None,
            on_result: Callable[[Any, str | None], None] | None = None,
        ) -> None:
            """Accept a turn and run it on a worker; output arrives over SSE.

            A turn can take minutes. Holding the request open for it makes every
            proxy and phone-network idle timeout a correctness problem, and the
            browser already reads results from the event stream, so the response
            carries only the acceptance. ``scope`` is a context-manager factory
            entered around the turn under the session lock (plan's OFF level);
            ``on_result(result, error)`` runs after it, still under the lock.
            """
            with entry.listeners_lock:
                if entry.closed:
                    self._send_error_json(410, "session closed")
                    return
                if entry.turn_active:
                    self._send_error_json(
                        409, f"a turn is already running (turn {entry.turn_id})"
                    )
                    return
                turn_id = uuid.uuid4().hex[:12]
                entry.turn_active = True
                entry.turn_id = turn_id

            def _run() -> None:
                try:
                    with entry.lock:
                        if entry.closed:
                            return
                        entry.session.done_checks = list(checks or [])
                        result, error = _run_scoped(
                            entry, message, model_override, max_time_s, scope,
                        )
                        entry.last_result = serve_board.outcome(result, error)
                        if error is not None:
                            print(f"[serve] turn {turn_id} failed: {error}",
                                  file=sys.stderr, flush=True)
                        if on_result is not None:
                            on_result(result, error)
                except Exception as e:  # noqa: BLE001 — a worker must not die silently
                    print(f"[serve] turn {turn_id} failed: {type(e).__name__}: {e}",
                          file=sys.stderr, flush=True)
                finally:
                    with entry.listeners_lock:
                        entry.turn_active = False
                        entry.turn_id = None

            threading.Thread(
                target=_run, name=f"turn-{turn_id}", daemon=True,
            ).start()
            self._send_json(202, {
                "session_id": session_id,
                "turn_id": turn_id,
                "status": "accepted",
            })

        def _handle_audit(self, session_id: str) -> None:
            from coding_harness.security import audit
            entries: list[dict[str, Any]] = []
            path = audit.AUDIT_PATH
            if path.exists():
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if row.get("session_id") == session_id:
                            entries.append(row)
            self._send_json(200, {
                "session_id": session_id,
                "entries": entries,
            })

        def _handle_list_permissions(self, entry: _SessionEntry) -> None:
            # Source of truth for pending JIT requests. A client reconciles
            # against this on connect + on interval, so a dropped SSE
            # permission_request costs poll latency, not a stuck turn.
            pending = entry.broker.list_pending() if entry.broker else []
            self._send_json(200, {
                "session_id": entry.session.session_id,
                "pending": pending,
            })

        def _handle_resolve_permission(
            self, session_id: str, req_id: str, body: dict[str, Any]
        ) -> None:
            entry = state.get(session_id)
            if entry is None:
                self._send_error_json(404, f"session not found: {session_id}")
                return
            if entry.broker is None:
                self._send_error_json(
                    409, "session has no permission broker (create with interactive:true)"
                )
                return
            decision = str(body.get("decision") or "").lower()
            if decision not in ("allow_once", "allow_always", "deny"):
                self._send_error_json(
                    400, "body.decision must be allow_once | allow_always | deny"
                )
                return
            expiry = body.get("expiry_minutes")
            if expiry is not None and not isinstance(expiry, (int, float)):
                self._send_error_json(400, "body.expiry_minutes must be a number")
                return
            # resolve() touches only broker.pending under broker.lock — never
            # entry.lock — so it unblocks a turn thread waiting in
            # broker.request() without any deadlock against /turn.
            ok = entry.broker.resolve(req_id, decision, expiry_minutes=expiry)
            if not ok:
                self._send_error_json(
                    404, f"no pending permission request: {req_id}"
                )
                return
            self._send_json(200, {"req_id": req_id, "decision": decision})

        def _handle_get_envelope(self, entry: _SessionEntry) -> None:
            self._send_json(200, {
                "session_id": entry.session.session_id,
                "identity": entry.identity,
                "envelope": entry.envelope.to_dict() if entry.envelope else None,
            })

        def _handle_resume(self, session_id: str, body: dict[str, Any]) -> None:
            interactive = body.get("interactive", False)
            if not isinstance(interactive, bool):
                self._send_error_json(400, "body.interactive must be a boolean")
                return
            entry, reason = state.resume_session(session_id, interactive=interactive)
            if entry is None:
                code = 404 if reason == "no_transcript" else 409
                self._send_error_json(code, f"cannot resume {session_id}: {reason}")
                return
            self._send_json(200, {
                "session_id": session_id,
                "status": reason,
                "mode": entry.session.mode.value,
                "autonomy": entry.session.registry.autonomy.value,
                "identity": entry.identity,
                "messages": len(entry.session.messages),
            })

        def _handle_probe(self, body: dict[str, Any]) -> None:
            """Start a background tool-call probe for one listed Ollama model."""
            from coding_harness.modes import model_probe

            model = body.get("model")
            listed = {m["id"]: m for m in ops.list_models()["models"] if m["backend"] == "ollama"}
            if not isinstance(model, str) or model not in listed:
                self._send_error_json(400, "body.model must name a listed Ollama model")
                return
            started = model_probe.start(model, listed[model].get("digest", ""))
            self._send_json(202, {"model": model, "status": "started" if started else "running"})

        def _handle_save_skill(self, name: str, body: dict[str, Any]) -> None:
            """Create or replace a skill under ~/.config/bjorn/skills."""
            description, text = body.get("description"), body.get("body")
            if not isinstance(description, str) or not isinstance(text, str):
                self._send_error_json(400, "body.description and body.body must be strings")
                return
            existing = serve_gui.skill_detail(name, os.getcwd())
            if existing is not None and not existing["editable"]:
                self._send_error_json(
                    409, f"{name} lives in {existing['source']}'s skills; copy it under a new name")
                return
            ok, reason = serve_gui.save_skill(name, description, text)
            if not ok:
                self._send_error_json(400, reason)
                return
            self._send_json(200, {"name": name, "path": reason})

        def _brain(self) -> Any:
            """The shared brain client when this server may use one, else None."""
            from coding_harness.context.brain import client_for

            return client_for(state.settings.brain) if state.brain else None

        def _handle_brain_get(self, path: str, query: dict[str, list[str]]) -> None:
            from coding_harness.context.brain import BrainError

            try:
                client = self._brain()
            except BrainError as e:
                if path == "/v1/brain/status":
                    self._send_json(200, serve_gui.brain_unavailable(str(e)))
                else:
                    self._send_error_json(502, str(e))
                return
            if path == "/v1/brain/status":
                self._send_json(200, serve_gui.brain_status(client))
                return
            if client is None:
                self._send_error_json(404, "no second brain configured")
                return
            try:
                if path == "/v1/brain/search":
                    q = (query.get("q") or [""])[0].strip()
                    if not q:
                        self._send_error_json(400, "q is required")
                        return
                    vault = (query.get("vault") or [""])[0] or None
                    self._send_json(200, serve_gui.brain_search(client, q, vault))
                elif path == "/v1/brain/page":
                    target = (query.get("path") or [""])[0]
                    if not target:
                        self._send_error_json(400, "path is required")
                        return
                    self._send_json(200, serve_gui.brain_page(client, target))
                else:
                    self._send_error_json(404, f"not found: {path}")
            except BrainError as e:
                self._send_error_json(502, str(e))

        def _handle_open_project(self, body: dict[str, Any]) -> None:
            """Find or start the GUI server for another project; the tab moves there."""
            target = body.get("path")
            if not isinstance(target, str) or not target:
                self._send_error_json(400, "body.path must be a non-empty string")
                return
            url, reason = serve_gui.open_project(
                target, model=state.model if state.explicit_model else None,
                force_local=state.force_local,
            )
            if url is None:
                code = 400 if reason == "not_a_listed_project" else 502
                self._send_error_json(code, f"cannot open {target}: {reason}")
                return
            self._send_json(200, {"url": url, "status": reason})

        def _handle_compact(self, session_id: str) -> None:
            """Fold older history into a summary now, the REPL's /compact."""
            entry = state.get(session_id)
            if entry is None:
                self._send_error_json(404, f"session not found: {session_id}")
                return
            with entry.listeners_lock:
                if entry.turn_active:
                    self._send_error_json(409, "a turn is running; compact after it ends")
                    return
                entry.turn_active = True  # holds turns off while the summary runs
            try:
                with entry.lock:
                    result = entry.session.compact()
            finally:
                with entry.listeners_lock:
                    entry.turn_active = False
            self._send_json(200, {
                "session_id": session_id,
                "compacted": result.compacted,
                "reason": result.reason,
                "messages_before": result.messages_before,
                "messages_after": result.messages_after,
            })

        def _adopt_settings(self, settings: Settings) -> None:
            """New sessions read the saved file; role models switch at once."""
            from coding_harness.core import model_roles

            state.settings = settings
            model_roles.configure(settings)

        def _handle_set_kind(self, session_id: str, body: dict[str, Any]) -> None:
            """Switch a session between code (tools, code model) and chat (no tools, chat model)."""
            entry = state.get(session_id)
            if entry is None:
                self._send_error_json(404, f"session not found: {session_id}")
                return
            kind = body.get("kind")
            if kind not in ("code", "chat"):
                self._send_error_json(400, "body.kind must be 'code' or 'chat'")
                return
            with entry.listeners_lock:
                if entry.turn_active:
                    self._send_error_json(409, "a turn is running; switch after it ends")
                    return
                entry.turn_active = True  # holds turns off; set_kind emits, which takes this lock
            try:
                entry.session.set_kind(kind)
            finally:
                with entry.listeners_lock:
                    entry.turn_active = False
            self._send_json(200, {"session_id": session_id, "kind": kind,
                                  "model": entry.session.model})

        def _handle_set_model(self, session_id: str, body: dict[str, Any]) -> None:
            """Pin every later turn to a model, or ``auto`` to hand back to the router."""
            entry = state.get(session_id)
            if entry is None:
                self._send_error_json(404, f"session not found: {session_id}")
                return
            tag = body.get("model")
            if not isinstance(tag, str) or not tag:
                self._send_error_json(400, "body.model must be a non-empty string")
                return
            if tag != "auto" and tag != "claude-cli":
                from coding_harness.models.ollama import (
                    BannedModelError,
                    assert_model_allowed,
                )
                try:
                    assert_model_allowed(tag)
                except BannedModelError as e:
                    self._send_error_json(400, str(e))
                    return
            with entry.listeners_lock:
                if entry.turn_active:
                    self._send_error_json(409, "a turn is running; switch after it ends")
                    return
                from coding_harness.models.profile import resolve_profile
                session = entry.session
                session.model = state.model if tag == "auto" else tag
                session.explicit_model = tag != "auto"
                session.profile = resolve_profile(session.model)
            self._send_json(200, {
                "session_id": session_id,
                "model": session.model,
                "explicit_model": session.explicit_model,
            })

        def _handle_interrupt(self, session_id: str) -> None:
            entry = state.get(session_id)
            if entry is None:
                self._send_error_json(404, f"session not found: {session_id}")
                return
            # interrupt() only sets an Event — no entry.lock needed, so it
            # lands even while a /turn holds the lock. Cancellation is
            # cooperative: it takes effect at the turn's next checkpoint, not
            # mid model-read. A turn parked in the JIT broker wait has no
            # checkpoint, so also deny every pending request — that unblocks
            # request() immediately with a deny instead of waiting out 300s.
            entry.session.interrupt()
            if entry.broker is not None:
                entry.broker.deny_all(reason="interrupt")
            self._send_json(202, {
                "session_id": session_id,
                "status": "interrupting",
                "note": "stops after the current in-flight step",
            })

        def _handle_revoke(self, session_id: str, body: dict[str, Any]) -> None:
            entry = state.get(session_id)
            if entry is None:
                self._send_error_json(404, f"session not found: {session_id}")
                return
            if entry.envelope is None:
                self._send_error_json(
                    409, "session has no envelope to revoke"
                )
                return
            reason = str(body.get("reason") or "operator_revoke")
            entry.envelope.revoked = True
            entry.session.interrupt()
            # Unblock any turn parked in the JIT wait and stop it re-entering
            # the broker on the now-revoked envelope (registry hard-denies
            # revoked without brokering).
            if entry.broker is not None:
                entry.broker.deny_all(reason="revoked")
            try:
                audit.append_envelope_change(
                    session_id=session_id,
                    action="revoke",
                    grant=None,
                    reason=reason,
                    agent_identity=entry.identity,
                )
            except Exception as e:  # noqa: BLE001 — audit best-effort
                sys.stderr.write(f"[serve] revoke audit error: {e}\n")
            entry.session._emit("envelope_revoked", {
                "session_id": session_id, "reason": reason,
            })
            self._send_json(200, {"session_id": session_id, "revoked": True})

        def _handle_events(self, entry: _SessionEntry) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self._send_cors_headers()
            self.end_headers()

            q = state.attach_listener(entry)
            try:
                self._stream_events(q, entry)
            finally:
                state.detach_listener(entry, q)

        def _stream_events(
            self, q: queue.Queue[dict[str, Any]], entry: _SessionEntry,
        ) -> None:
            last_keepalive = time.time()
            # UNBOUNDED-LOOP: serves the SSE stream until the client closes
            # the socket or the session is torn down. Both surface as
            # write failures, ending the loop.
            while not entry.closed:
                try:
                    payload = q.get(timeout=1.0)
                except queue.Empty:
                    if time.time() - last_keepalive > SSE_KEEPALIVE_SECONDS:
                        try:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionError, OSError):
                            return
                        last_keepalive = time.time()
                    continue
                # JSON-RPC 2.0 notification envelope: method = event kind,
                # params = the rest of the payload.
                # The same dict sits in every listener's queue and in the
                # replay history, so read it without mutating it.
                jsonrpc = {
                    "jsonrpc": "2.0",
                    "method": payload.get("event", "unknown"),
                    "params": {k: v for k, v in payload.items() if k != "event"},
                }
                line = f"data: {json.dumps(jsonrpc, default=str)}\n\n"
                try:
                    self.wfile.write(line.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionError, OSError):
                    return
                last_keepalive = time.time()

    return _Handler


# ── OpenAPI spec ─────────────────────────────────────────────────────

def _hooked_turn(
    entry: _SessionEntry, message: str, model_override: str | None,
    max_time_s: float | None,
) -> Any:
    """One turn between the UserPromptSubmit and Stop hooks.

    Same shape as print mode: a Stop hook that blocks feeds its reason back
    as one extra turn. With no hooks configured this is exactly ``run_turn``.
    """
    def _run(text: str) -> Any:
        return entry.session.run_turn(
            text, model_override=model_override, deadline_s=max_time_s,
        )

    hooks = entry.hooks
    if hooks is None:
        return _run(message)
    hooks.run("UserPromptSubmit")
    result = _run(message)
    stop = hooks.run("Stop")
    if stop.stop_messages:
        result = _run("\n".join(stop.stop_messages))
    return result


def _run_scoped(
    entry: _SessionEntry, message: str, model_override: str | None,
    max_time_s: float | None, scope: Callable[[], Any] | None,
) -> tuple[Any, str | None]:
    """One turn inside ``scope``: (result, None), or (None, error text)."""
    try:
        with (scope() if scope is not None else nullcontext()):
            result = _hooked_turn(entry, message, model_override, max_time_s)
        return result, None
    except Exception as e:  # noqa: BLE001 — reported to on_result, not raised
        return None, f"{type(e).__name__}: {e}"


def _positive_number(value: Any) -> bool:
    """True for a JSON number above zero (a JSON boolean is not a number)."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
    )


def _autonomy_from_body(body: dict[str, Any]) -> tuple[Autonomy | None, str | None]:
    """(level, None) for a valid or absent body.autonomy, else (None, error)."""
    raw = body.get("autonomy")
    if raw is None:
        return None, None
    if not isinstance(raw, str):
        return None, "body.autonomy must be a string"
    try:
        return parse_autonomy(raw), None
    except ValueError as e:
        return None, f"invalid autonomy: {e}"
def _string_list(value: Any) -> bool:
    """True for a JSON array whose every item is a string."""
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


_OPENAPI_SPEC: dict[str, Any] = {
    "openapi": "3.0.3",
    "info": {
        "title": "coding_harness serve",
        "version": "0.1.0",
        "description": (
            "HTTP+SSE surface for the coding harness. Plan mode by default; "
            "Act mode requires explicit opt-in at session creation."
        ),
    },
    "paths": {
        "/v1/healthz": {
            "get": {
                "summary": "Process liveness",
                "responses": {"200": {"description": "ok"}},
            }
        },
        "/v1/openapi.json": {
            "get": {
                "summary": "Self-description",
                "responses": {"200": {"description": "OpenAPI 3.x"}},
            }
        },
        "/v1/routines": {
            "get": {
                "summary": "Heartbeat scheduled jobs + failure state (read-only)",
                "responses": {"200": {"description": "routines + summary"}},
            }
        },
        "/v1/work": {
            "get": {
                "summary": "Beads work items (read-only; ?status=open|closed)",
                "responses": {"200": {"description": "work items + source"}},
            }
        },
        "/v1/models": {
            "get": {
                "summary": "Models a turn may be pinned to (allowed-origin only)",
                "responses": {"200": {"description": "model list"}},
            }
        },
        "/v1/sessions": {
            "get": {
                "summary": "List sessions (newest first)",
                "responses": {"200": {"description": "session summaries"}},
            },
            "post": {
                "summary": "Create a session",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "mode": {
                                        "type": "string",
                                        "enum": ["plan", "act"],
                                        "default": "plan",
                                    },
                                    "autonomy": {
                                        "type": "string",
                                        "enum": ["off", "low", "medium", "high"],
                                        "default": "low",
                                        "description": (
                                            "Shell command set the session may "
                                            "run without asking; commands "
                                            "outside it are denied unless the "
                                            "session is interactive. When set, "
                                            "the mode follows it (off is plan)."
                                        ),
                                    },
                                    "identity": {
                                        "type": "string",
                                        "description": (
                                            "Agent name; resolved against "
                                            "agents.yaml to a carryall:{name}#{key} "
                                            "DID stamped on every audit row."
                                        ),
                                    },
                                    "interactive": {
                                        "type": "boolean",
                                        "default": False,
                                        "description": (
                                            "Attach a JIT permission broker so "
                                            "out-of-envelope calls wait for an "
                                            "operator decision. Headless callers "
                                            "leave this off: denials return at once."
                                        ),
                                    },
                                    "envelope": {
                                        "type": "object",
                                        "description": (
                                            "Deny-by-default scope. Omit for the "
                                            "default preset: read and write under "
                                            "cwd, Bash at tool level."
                                        ),
                                        "properties": {
                                            "grants": {
                                                "type": "array",
                                                "items": {
                                                    "type": "object",
                                                    "properties": {
                                                        "tool": {"type": "string"},
                                                        "access": {
                                                            "type": "string",
                                                            "enum": ["read", "write", "execute"],
                                                        },
                                                        "path_glob": {"type": "string"},
                                                        "expiry_minutes": {"type": "number"},
                                                    },
                                                    "required": ["tool"],
                                                },
                                            },
                                            "expiry_minutes": {"type": "number"},
                                        },
                                    },
                                },
                            }
                        }
                    }
                },
                "responses": {"201": {"description": "created"}},
            }
        },
        "/v1/sessions/{id}/turn": {
            "post": {
                "summary": "Run one prompt",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["message"],
                                "properties": {
                                    "message": {"type": "string"},
                                    "model": {"type": "string"},
                                    "wait": {
                                        "type": "boolean",
                                        "default": False,
                                        "description": (
                                            "false (default) accepts the turn and "
                                            "returns 202 with a turn_id; results "
                                            "arrive on the event stream. true holds "
                                            "the response open for the whole turn and "
                                            "returns the full result — convenient for "
                                            "scripts, fragile across proxy timeouts"
                                        ),
                                    },
                                    "max_time_s": {
                                        "type": "number",
                                        "minimum": 0,
                                        "exclusiveMinimum": True,
                                        "description": (
                                            "wall-clock budget for the turn; "
                                            "on expiry halted_reason=deadline "
                                            "and files_changed lists the work"
                                        ),
                                    },
                                    "checks": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "description": (
                                            "shell commands the green-before-done "
                                            "gate runs before the turn finishes; "
                                            "omitted or empty discovers the repo's "
                                            "test command. The response carries "
                                            "checks_passed and checks_report."
                                        ),
                                    },
                                },
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "assistant turn"}},
            }
        },
        "/v1/sessions/{id}/audit": {
            "get": {
                "summary": "Session audit entries",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "audit rows"}},
            }
        },
        "/v1/sessions/{id}/events": {
            "get": {
                "summary": "SSE stream of session events (JSON-RPC notifications)",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {
                        "description": "text/event-stream of JSON-RPC 2.0 notifications",
                    }
                },
            }
        },
        "/v1/sessions/{id}/envelope": {
            "get": {
                "summary": "Current session envelope + identity",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "envelope snapshot"}},
            }
        },
        "/v1/sessions/{id}/permissions": {
            "get": {
                "summary": "List pending JIT permission requests (source of truth)",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "pending requests"}},
            }
        },
        "/v1/sessions/{id}/permissions/{req_id}": {
            "post": {
                "summary": "Resolve a pending permission request",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                    {"name": "req_id", "in": "path", "required": True,
                     "schema": {"type": "string"}},
                ],
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["decision"],
                                "properties": {
                                    "decision": {
                                        "type": "string",
                                        "enum": ["allow_once", "allow_always", "deny"],
                                    },
                                    "expiry_minutes": {"type": "number"},
                                },
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "resolved"}},
            }
        },
        "/v1/sessions/{id}/resume": {
            "post": {
                "summary": "Rebuild a session from its on-disk transcript",
                "description": (
                    "Idempotent. 409 when the transcript predates tool-result "
                    "logging or its envelope has lapsed — a resumed session "
                    "never holds more authority than the one it continues."
                ),
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "responses": {
                    "200": {"description": "resumed or already live"},
                    "404": {"description": "no transcript"},
                    "409": {"description": "not resumable"},
                },
            }
        },
        "/v1/sessions/{id}/interrupt": {
            "post": {
                "summary": "Cooperatively cancel the in-flight turn",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "responses": {"202": {"description": "interrupting"}},
            }
        },
        "/v1/sessions/{id}/revoke": {
            "post": {
                "summary": "Revoke the envelope + interrupt the session",
                "parameters": [
                    {"name": "id", "in": "path", "required": True,
                     "schema": {"type": "string"}}
                ],
                "responses": {"200": {"description": "revoked"}},
            }
        },
    },
}
_OPENAPI_SPEC["paths"].update(serve_plan.OPENAPI_PATHS)
_OPENAPI_SPEC["paths"].update(serve_brain_extra.OPENAPI_PATHS)
_OPENAPI_SPEC["paths"].update(serve_board.OPENAPI_PATHS)


_OPENAPI_SPEC["paths"].update(serve_changes.OPENAPI_PATHS)
_OPENAPI_SPEC["paths"].update(serve_git.OPENAPI_PATHS)
_OPENAPI_SPEC["paths"].update(serve_history.OPENAPI_PATHS)
_OPENAPI_SPEC["paths"].update(serve_settings.OPENAPI_PATHS)


# ── Entry point ──────────────────────────────────────────────────────


class _Server(ThreadingHTTPServer):
    """HTTPServer.server_bind reverse-resolves the hostname; a slow resolver
    holds every startup for 30s plus, so bind without the lookup."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def run(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    model: str | None = None,
    force_local: bool = False,
    explicit_model: bool = False,
    enable_mcp: bool = True,
    ui_origin: str = DEFAULT_UI_ORIGIN,
    frontier_backend: str = "api",
    ready_callback: Callable[[ThreadingHTTPServer], None] | None = None,
    settings: Settings | None = None,
    brain: bool = False,
) -> int:
    """Start the HTTP server and block until interrupted.

    ``ready_callback`` is invoked once the socket is bound and the server
    is ready to serve. Tests use it to grab the assigned port (when
    ``port=0``) and to signal a barrier so the test thread can drive
    requests against the server without sleeping.
    """
    from coding_harness.modes.print_mode import DEFAULT_MODEL
    state = _ServerState(
        model=model or DEFAULT_MODEL,
        force_local=force_local,
        explicit_model=explicit_model,
        enable_mcp=enable_mcp,
        ui_origin=ui_origin,
        frontier_backend=frontier_backend,
        settings=settings,
        brain=brain,
    )
    state.auth = serve_auth.AuthGate(host)
    handler_cls = _make_handler(state)
    server = _Server((host, port), handler_cls)
    server.daemon_threads = True

    if ready_callback is not None:
        ready_callback(server)

    print(
        f"[coding_harness] serve listening on http://{host}:{server.server_port}",
        file=sys.stderr, flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        state.close()
    return 0


def _new_session_id() -> str:  # pragma: no cover — utility for tests
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{uuid.uuid4().hex[:8]}"
