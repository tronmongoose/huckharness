"""Tests for the HTTP+SSE serve mode.

Spins up a real ``ThreadingHTTPServer`` on an ephemeral port (``--port 0``)
and drives requests against it from the test thread. The Ollama call is
mocked so a "turn" returns immediately without any model traffic.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from coding_harness.core.mode import Mode
from coding_harness.core.session import SessionResult
from coding_harness.modes import serve_mode
from coding_harness.security import audit


def _fake_ollama_chat(*, model, messages, tools, on_delta=None, timeout=None, **_kw):
    """Stand-in for ``coding_harness.models.ollama.chat`` that returns
    a no-tool-call assistant message — terminates the agent loop in one
    iteration so tests don't need any real model traffic.

    Accepts ``on_delta`` (serve always passes it, since serve sessions have
    an event_sink). When present, streams the reply word-by-word so the
    streaming path — and the interrupt checkpoint inside ``on_delta`` — is
    exercised end-to-end. ``timeout`` arrives only when the turn carries a
    ``max_time_s`` deadline."""
    content = f"echo: {messages[-1]['content']}"
    if on_delta is not None:
        for token in content.split(" "):
            on_delta(token + " ")
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [],
        "_usage": {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None},
    }


def _start_server() -> tuple[ThreadingHTTPServer, threading.Thread, int]:
    barrier: dict[str, ThreadingHTTPServer | None] = {"server": None}
    started = threading.Event()

    def _ready(server: ThreadingHTTPServer) -> None:
        barrier["server"] = server
        started.set()

    thread = threading.Thread(
        target=serve_mode.run,
        kwargs=dict(
            host="127.0.0.1",
            port=0,
            enable_mcp=False,  # tests don't want subprocesses
            ready_callback=_ready,
        ),
        daemon=True,
    )
    thread.start()
    if not started.wait(timeout=5.0):
        raise RuntimeError("serve_mode.run never invoked ready_callback")
    server = barrier["server"]
    assert server is not None
    return server, thread, server.server_port


def _request(method: str, url: str, *, body: dict | None = None, timeout: float = 5.0):
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


class TestServeMode(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls.tmp.name)
        # Redirect audit writes to a tmp dir so test runs don't pollute the
        # real <meta_dir>/audit.jsonl chain.
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", cls.tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", cls.tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", cls.tmp_path),
            mock.patch(
                "coding_harness.core.session.ollama.chat",
                side_effect=_fake_ollama_chat,
            ),
            # Stub Sentinel — we're not testing it here, the harness suite does.
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(
                    allowed=True, reason="test", path="hook"
                ),
            ),
        ]
        for p in cls.patches:
            p.start()

        cls.server, cls.thread, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    # ── Surface ──────────────────────────────────────────────────

    def test_healthz(self) -> None:
        status, body = _request("GET", self._url("/v1/healthz"))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("active_sessions", payload)

    def test_openapi(self) -> None:
        status, body = _request("GET", self._url("/v1/openapi.json"))
        self.assertEqual(status, 200)
        spec = json.loads(body)
        self.assertEqual(spec["openapi"][0:3], "3.0")
        self.assertIn("/v1/sessions", spec["paths"])
        self.assertIn("/v1/sessions/{id}/turn", spec["paths"])
        self.assertIn("/v1/sessions/{id}/events", spec["paths"])
        self.assertIn("/v1/sessions/{id}/envelope", spec["paths"])
        self.assertIn("/v1/sessions/{id}/permissions", spec["paths"])
        self.assertIn("/v1/sessions/{id}/permissions/{req_id}", spec["paths"])
        self.assertIn("/v1/sessions/{id}/interrupt", spec["paths"])
        self.assertIn("/v1/sessions/{id}/revoke", spec["paths"])
        self.assertIn("/v1/routines", spec["paths"])
        self.assertIn("/v1/work", spec["paths"])

    def test_routines_endpoint(self) -> None:
        # Point ops at a synthetic heartbeat dir: one healthy cron job, one
        # failing interval job, no scheduler pid (⇒ not alive).

        from coding_harness.modes import ops
        with tempfile.TemporaryDirectory() as tmp:
            hb = Path(tmp)
            (hb / "config.yaml").write_text(
                "jobs:\n"
                "  good_job:\n"
                "    schedule: cron\n"
                "    hour: 7\n"
                "    minute: 30\n"
                "    command: make good\n"
                "    description: a good job\n"
                "  bad_job:\n"
                "    schedule: interval\n"
                "    minutes: 5\n"
                "    command: make bad\n"
                "    description: a failing job\n"
            )
            (hb / "failure_state.json").write_text(
                json.dumps({"bad_job": {
                    "count": 3, "last_failure": "2026-07-11T00:00:00Z",
                    "last_error": "boom",
                }})
            )
            with mock.patch.object(ops, "HEARTBEAT_DIR", hb):
                status, body = _request("GET", self._url("/v1/routines"))
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["failed"], 1)
        self.assertFalse(payload["scheduler_alive"])
        # Failed jobs sort first.
        self.assertEqual(payload["jobs"][0]["id"], "bad_job")
        self.assertEqual(payload["jobs"][0]["consecutive_failures"], 3)
        self.assertEqual(payload["jobs"][0]["last_error"], "boom")
        self.assertEqual(payload["jobs"][1]["cadence"], "daily 07:30")

    def test_work_endpoint_backup_fallback(self) -> None:

        from coding_harness.modes import ops
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / "issues.jsonl"
            backup.write_text(
                json.dumps({
                    "id": "sl-aaaa", "title": "open thing", "status": "open",
                    "priority": 1, "issue_type": "task",
                    "updated_at": "2026-07-10T00:00:00Z",
                }) + "\n" + json.dumps({
                    "id": "sl-bbbb", "title": "closed thing",
                    "status": "closed", "priority": 2, "issue_type": "bug",
                    "updated_at": "2026-07-09T00:00:00Z",
                }) + "\n"
            )
            with mock.patch.object(ops, "BEADS_BACKUP", backup), \
                 mock.patch.object(ops, "_work_from_bd", return_value=None):
                status, body = _request("GET", self._url("/v1/work"))
                status_c, body_c = _request(
                    "GET", self._url("/v1/work?status=closed")
                )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["source"], "backup")
        self.assertEqual([i["id"] for i in payload["items"]], ["sl-aaaa"])
        self.assertEqual(payload["items"][0]["issue_type"], "task")
        payload_c = json.loads(body_c)
        self.assertEqual(status_c, 200)
        self.assertEqual([i["id"] for i in payload_c["items"]], ["sl-bbbb"])

    def test_models_endpoint(self) -> None:
        fake_tags = json.dumps({"models": [
            {"name": "mistral-small3.2:latest"},
            {"name": "qwen2:7b"},  # banned origin — must be omitted
        ]}).encode()

        class _FakeResp:
            def read(self):
                return fake_tags
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        with mock.patch(
            "coding_harness.modes.ops.urlopen", return_value=_FakeResp()
        ):
            status, body = _request("GET", self._url("/v1/models"))
        self.assertEqual(status, 200)
        ids = [m["id"] for m in json.loads(body)["models"]]
        self.assertIn("mistral-small3.2:latest", ids)
        self.assertIn("claude-cli", ids)
        self.assertNotIn("qwen2:7b", ids)

    def test_turn_rejects_banned_model_override(self) -> None:
        _, create = _request(
            "POST", self._url("/v1/sessions"), body={"mode": "act"}
        )
        sid = json.loads(create)["session_id"]
        status, body = _request(
            "POST", self._url(f"/v1/sessions/{sid}/turn"),
            body={"message": "hi", "model": "deepseek-r1:8b"},
        )
        self.assertEqual(status, 400)
        self.assertIn("banned", json.loads(body)["error"]["message"])

    def test_404_on_unknown_api_path(self) -> None:
        # Non-API paths are the SPA's now and answer 200 or 503 depending on
        # whether ui/dist is built — test_serve_ui.py pins that with its own
        # UI_DIST. The API's own 404 contract is what belongs here.
        status, body = _request("GET", self._url("/v1/nope"))
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], 404)

    def test_cors_headers_present(self) -> None:
        req = urllib.request.Request(self._url("/v1/healthz"))
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            self.assertEqual(
                resp.headers.get("Access-Control-Allow-Origin"),
                "http://localhost:5173",
            )

    def test_options_preflight(self) -> None:
        req = urllib.request.Request(
            self._url("/v1/sessions"), method="OPTIONS"
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(
                resp.headers.get("Access-Control-Allow-Origin"),
                "http://localhost:5173",
            )
            self.assertIn("POST", resp.headers.get("Access-Control-Allow-Methods", ""))

    def test_list_sessions(self) -> None:
        _, create = _request(
            "POST", self._url("/v1/sessions"), body={"identity": "lister"}
        )
        sid = json.loads(create)["session_id"]
        status, body = _request("GET", self._url("/v1/sessions"))
        self.assertEqual(status, 200)
        sessions = json.loads(body)["sessions"]
        mine = [s for s in sessions if s["session_id"] == sid]
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]["identity"], "carryall:lister")
        self.assertIn("mode", mine[0])
        self.assertIn("revoked", mine[0])

    # ── Session lifecycle ────────────────────────────────────────

    def test_create_session_defaults_to_plan(self) -> None:
        status, body = _request("POST", self._url("/v1/sessions"), body={})
        self.assertEqual(status, 201)
        payload = json.loads(body)
        self.assertEqual(payload["mode"], "plan")
        self.assertTrue(payload["session_id"])

    def test_create_session_explicit_act(self) -> None:
        status, body = _request(
            "POST", self._url("/v1/sessions"), body={"mode": "act"}
        )
        self.assertEqual(status, 201)
        self.assertEqual(json.loads(body)["mode"], "act")

    def test_create_session_invalid_mode(self) -> None:
        status, body = _request(
            "POST", self._url("/v1/sessions"), body={"mode": "bogus"}
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid mode", json.loads(body)["error"]["message"])

    def test_turn_returns_assistant_text(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        session_id = json.loads(create)["session_id"]
        status, body = _request(
            "POST", self._url(f"/v1/sessions/{session_id}/turn"),
            body={"message": "hello", "wait": True},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["session_id"], session_id)
        self.assertEqual(payload["text"], "echo: hello")
        self.assertEqual(payload["halted_reason"], "model_done")

    def test_turn_response_shape_is_nightshift_compatible(self) -> None:
        # Nightshift's circuit breaker reads this exact key set off
        # the /turn response. Guard the shape so an added field or a rename
        # can't silently break the backlog worker.
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        session_id = json.loads(create)["session_id"]
        status, body = _request(
            "POST", self._url(f"/v1/sessions/{session_id}/turn"),
            body={"message": "shape check", "wait": True},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(
            set(payload.keys()),
            {
                "session_id", "text", "turns", "halted_reason",
                "error", "tokens_in", "tokens_out", "files_changed",
                "checks_passed", "checks_report",
            },
        )

    def test_turn_unknown_session(self) -> None:
        status, _ = _request(
            "POST", self._url("/v1/sessions/no-such-id/turn"),
            body={"message": "x"},
        )
        self.assertEqual(status, 404)

    def test_turn_requires_message(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        session_id = json.loads(create)["session_id"]
        status, _ = _request(
            "POST", self._url(f"/v1/sessions/{session_id}/turn"),
            body={},
        )
        self.assertEqual(status, 400)

    def test_audit_endpoint_returns_session_entries(self) -> None:
        # Drive a turn so a turn audit entry is produced.
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        session_id = json.loads(create)["session_id"]
        _request(
            "POST", self._url(f"/v1/sessions/{session_id}/turn"),
            body={"message": "hi", "wait": True},
        )
        status, body = _request(
            "GET", self._url(f"/v1/sessions/{session_id}/audit")
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["session_id"], session_id)
        # At minimum the per-turn audit row should be present.
        self.assertGreaterEqual(len(payload["entries"]), 1)
        self.assertTrue(
            all(e["session_id"] == session_id for e in payload["entries"])
        )


    # ── Per-turn deadline (P0-1) ─────────────────────────────────

    def test_turn_with_max_time_reports_files_changed(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        session_id = json.loads(create)["session_id"]
        status, body = _request(
            "POST", self._url(f"/v1/sessions/{session_id}/turn"),
            body={"message": "budgeted", "max_time_s": 30, "wait": True},
        )
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertEqual(payload["halted_reason"], "model_done")
        self.assertEqual(payload["files_changed"], [])

    def test_turn_rejects_bad_max_time(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        session_id = json.loads(create)["session_id"]
        for bad in ("30", 0, -1, True):
            status, body = _request(
                "POST", self._url(f"/v1/sessions/{session_id}/turn"),
                body={"message": "budgeted", "max_time_s": bad},
            )
            self.assertEqual(status, 400, (bad, body))
            self.assertIn("max_time_s", json.loads(body)["error"]["message"])

    # ── Green-before-done gate (P1-4) ────────────────────────────

    def test_turn_rejects_non_list_checks(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        session_id = json.loads(create)["session_id"]
        for bad in ("pytest -q", [1], {"cmd": "x"}, [["pytest"]], ["ok", None]):
            status, body = _request(
                "POST", self._url(f"/v1/sessions/{session_id}/turn"),
                body={"message": "gated", "checks": bad},
            )
            self.assertEqual(status, 400, (bad, body))
            self.assertIn("checks", json.loads(body)["error"]["message"])

    def test_turn_accepts_checks_and_carries_the_verdict(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        session_id = json.loads(create)["session_id"]
        seen: list = []

        def fake_turn(session, message, model_override=None, deadline_s=None):
            seen.append(list(session.done_checks))
            return SessionResult(
                final_text="ok", turns=1, session_id=session.session_id,
                session_log_path=self.tmp_path / "s.jsonl", halted_reason="model_done",
                checks_passed=False, checks_report="FAIL: make test",
            )

        turn = self._url(f"/v1/sessions/{session_id}/turn")
        with mock.patch.object(serve_mode.Session, "run_turn", autospec=True, side_effect=fake_turn):
            status, body = _request(
                "POST", turn,
                body={"message": "gated", "checks": ["make test", "pytest -q"], "wait": True},
            )
            self.assertEqual(status, 200, body)
            self.assertEqual(
                _request("POST", turn, body={"message": "again", "wait": True})[0], 200,
            )
        payload = json.loads(body)
        self.assertEqual((payload["checks_passed"], payload["checks_report"]), (False, "FAIL: make test"))
        # An omitted list resets to discovery: checks bind to one turn, not the session.
        self.assertEqual(seen, [["make test", "pytest -q"], []])
        spec = json.loads(_request("GET", self._url("/v1/openapi.json"))[1])
        turn_schema = spec["paths"]["/v1/sessions/{id}/turn"]["post"]["requestBody"]
        props = turn_schema["content"]["application/json"]["schema"]["properties"]
        self.assertEqual(props["checks"]["items"], {"type": "string"})


class TestServeModeSSEEvents(unittest.TestCase):
    """Verify the SSE stream emits JSON-RPC notifications for events.

    Run independently from the lifecycle tests so a slow listener doesn't
    interfere with them.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", cls.tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", cls.tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", cls.tmp_path),
            mock.patch(
                "coding_harness.core.session.ollama.chat",
                side_effect=_fake_ollama_chat,
            ),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(
                    allowed=True, reason="test", path="hook"
                ),
            ),
        ]
        for p in cls.patches:
            p.start()
        cls.server, cls.thread, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def test_events_stream_sees_session_start(self) -> None:
        # Create a session, then subscribe to its event stream in a
        # background thread, then drive a turn — assert the session_start
        # / route_decision events arrive on the stream.
        _, create = _request(
            "POST", f"http://127.0.0.1:{self.port}/v1/sessions", body={}
        )
        session_id = json.loads(create)["session_id"]

        events: list[dict] = []
        done = threading.Event()

        def _consume() -> None:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}/v1/sessions/{session_id}/events"
            )
            with urllib.request.urlopen(req, timeout=5.0) as resp:
                buffer = b""
                while not done.is_set():
                    chunk = resp.read(1)
                    if not chunk:
                        break
                    buffer += chunk
                    if buffer.endswith(b"\n\n"):
                        for line in buffer.decode("utf-8").splitlines():
                            if line.startswith("data: "):
                                events.append(json.loads(line[len("data: "):]))
                        buffer = b""
                        if len(events) >= 2:
                            done.set()
                            return

        consumer = threading.Thread(target=_consume, daemon=True)
        consumer.start()
        # Give the consumer's GET a moment to attach as a listener.
        time.sleep(0.1)

        _request(
            "POST", f"http://127.0.0.1:{self.port}/v1/sessions/{session_id}/turn",
            body={"message": "hi events"},
        )

        done.wait(timeout=3.0)
        # We should have seen at least the JSON-RPC envelope shape.
        self.assertGreater(len(events), 0, "no SSE events captured")
        for ev in events:
            self.assertEqual(ev["jsonrpc"], "2.0")
            self.assertIsInstance(ev["method"], str)
            self.assertIsInstance(ev["params"], dict)


class TestServeModeGovernance(unittest.TestCase):
    """Envelope + identity + JIT-permission + interrupt endpoints (P1)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.tmp_path = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", cls.tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", cls.tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", cls.tmp_path),
            mock.patch(
                "coding_harness.core.session.ollama.chat",
                side_effect=_fake_ollama_chat,
            ),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(
                    allowed=True, reason="test", path="hook"
                ),
            ),
        ]
        for p in cls.patches:
            p.start()
        cls.server, cls.thread, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _create(self, body: dict) -> dict:
        status, resp = _request("POST", self._url("/v1/sessions"), body=body)
        self.assertEqual(status, 201, resp)
        return json.loads(resp)

    def test_create_with_identity_and_envelope(self) -> None:
        payload = self._create({
            "mode": "act",
            "identity": "test-agent",
            "envelope": {
                "grants": [{"tool": "Read", "access": "read", "path_glob": "/tmp/**"}],
            },
        })
        self.assertEqual(payload["identity"], "carryall:test-agent")
        self.assertIsNotNone(payload["envelope"])
        self.assertEqual(len(payload["envelope"]["grants"]), 1)

    def test_get_envelope_reflects_session(self) -> None:
        created = self._create({
            "identity": "reader",
            "envelope": {"grants": [{"tool": "Read", "path_glob": "/x/**"}]},
        })
        sid = created["session_id"]
        status, body = _request("GET", self._url(f"/v1/sessions/{sid}/envelope"))
        self.assertEqual(status, 200)
        env = json.loads(body)
        self.assertEqual(env["identity"], "carryall:reader")
        self.assertEqual(env["envelope"]["grants"][0]["tool"], "Read")

    def test_permissions_empty_when_no_pending(self) -> None:
        created = self._create({
            "envelope": {"grants": [{"tool": "Read", "path_glob": "/x/**"}]},
        })
        sid = created["session_id"]
        status, body = _request("GET", self._url(f"/v1/sessions/{sid}/permissions"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["pending"], [])

    def test_resolve_unknown_request_404(self) -> None:
        created = self._create({
            "envelope": {"grants": [{"tool": "Read", "path_glob": "/x/**"}]},
            "interactive": True,
        })
        sid = created["session_id"]
        status, _ = _request(
            "POST", self._url(f"/v1/sessions/{sid}/permissions/nope"),
            body={"decision": "allow_once"},
        )
        self.assertEqual(status, 404)

    def test_resolve_without_broker_409(self) -> None:
        # Not interactive ⇒ no broker, even though the preset envelope exists.
        created = self._create({})
        sid = created["session_id"]
        status, _ = _request(
            "POST", self._url(f"/v1/sessions/{sid}/permissions/anything"),
            body={"decision": "allow_once"},
        )
        self.assertEqual(status, 409)

    def test_interrupt_unknown_session_404(self) -> None:
        status, _ = _request("POST", self._url("/v1/sessions/no-id/interrupt"))
        self.assertEqual(status, 404)

    def test_interrupt_idle_session_accepted(self) -> None:
        sid = self._create({})["session_id"]
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/interrupt"))
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(body)["status"], "interrupting")

    def test_revoke_default_session_revokes_preset(self) -> None:
        sid = self._create({})["session_id"]
        status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/revoke"))
        self.assertEqual(status, 200)
        _, env_body = _request("GET", self._url(f"/v1/sessions/{sid}/envelope"))
        self.assertTrue(json.loads(env_body)["envelope"]["revoked"])

    def test_default_session_has_preset_envelope(self) -> None:
        created = self._create({})
        env = created["envelope"]
        self.assertIsNotNone(env)
        self.assertEqual(
            {g["tool"] for g in env["grants"]},
            {"Read", "Grep", "Glob", "Write", "Edit", "Bash"},
        )
        root = os.path.realpath(os.getcwd())
        write = next(g for g in env["grants"] if g["tool"] == "Write")
        self.assertEqual(write["path_glob"], f"{root}/**")
        bash = next(g for g in env["grants"] if g["tool"] == "Bash")
        self.assertIsNone(bash["expires_at"])

    def test_broker_only_when_interactive(self) -> None:
        plain = self._create({})["session_id"]
        status, _ = _request(
            "POST", self._url(f"/v1/sessions/{plain}/permissions/x"),
            body={"decision": "deny"},
        )
        self.assertEqual(status, 409)
        interactive = self._create({"interactive": True})["session_id"]
        status, _ = _request(
            "POST", self._url(f"/v1/sessions/{interactive}/permissions/x"),
            body={"decision": "deny"},
        )
        # A broker exists, so the unknown request id is the only complaint.
        self.assertEqual(status, 404)

    def test_interactive_must_be_boolean(self) -> None:
        status, body = _request(
            "POST", self._url("/v1/sessions"), body={"interactive": "yes"},
        )
        self.assertEqual(status, 400)
        self.assertIn("interactive", json.loads(body)["error"]["message"])

    def test_out_of_envelope_write_denies_fast_without_broker(self) -> None:
        state = serve_mode._ServerState(
            model="test-model", force_local=True, explicit_model=False, enable_mcp=False,
        )
        entry = state.create_session(mode=Mode.ACT)
        self.assertIsNone(entry.broker)
        outside = os.path.join(tempfile.gettempdir(), "p06-outside-envelope.txt")
        started = time.monotonic()
        result = entry.session.registry.dispatch(
            "Write", {"file_path": outside, "content": "x"},
        )
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(result.is_error)
        self.assertIn("BLOCKED by envelope", result.content)
        self.assertFalse(os.path.exists(outside))
        entry.session.close()

    def test_non_object_body_400(self) -> None:
        # A bare JSON array must 400, not drop the connection.
        req = urllib.request.Request(
            self._url("/v1/sessions"),
            data=b"[1,2,3]",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            urllib.request.urlopen(req, timeout=5.0)
            self.fail("expected HTTPError")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_invalid_envelope_spec_400(self) -> None:
        status, body = _request(
            "POST", self._url("/v1/sessions"),
            body={"envelope": {"grants": [{"access": "read"}]}},  # missing tool
        )
        self.assertEqual(status, 400)
        self.assertIn("invalid envelope", json.loads(body)["error"]["message"])

    def test_negative_expiry_rejected(self) -> None:
        status, _ = _request(
            "POST", self._url("/v1/sessions"),
            body={"envelope": {"grants": [{"tool": "Read"}], "expiry_minutes": -1}},
        )
        self.assertEqual(status, 400)

    def test_revoke_marks_envelope_and_audits(self) -> None:
        created = self._create({
            "envelope": {"grants": [{"tool": "Read", "path_glob": "/x/**"}]},
        })
        sid = created["session_id"]
        status, body = _request("POST", self._url(f"/v1/sessions/{sid}/revoke"))
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["revoked"])
        # Envelope now reports revoked.
        _, env_body = _request("GET", self._url(f"/v1/sessions/{sid}/envelope"))
        self.assertTrue(json.loads(env_body)["envelope"]["revoked"])
        # An envelope_change/revoke row is on the audit chain.
        rows = [
            json.loads(line)
            for line in (self.tmp_path / "audit.jsonl").read_text().splitlines()
            if line.strip()
        ]
        self.assertTrue(
            any(r.get("kind") == "envelope_change" and r.get("action") == "revoke"
                for r in rows)
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
