"""Serving the built SPA from the harness itself.

One origin is the whole point: `bjorn serve` plus a browser tab, with no Vite
dev server and no CORS. The API keeps precedence — a mistyped /v1 route must
answer JSON, not the app shell — and nothing outside ui/dist may be read.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from coding_harness.core.session import SessionResult
from coding_harness.modes import serve_mode

_INDEX = "<!doctype html><title>bjorn</title><div id=root></div>"
_JS = "console.log('hi')\n"


def _start_server() -> tuple[ThreadingHTTPServer, int]:
    barrier: dict[str, ThreadingHTTPServer | None] = {"server": None}
    started = threading.Event()

    def _ready(server: ThreadingHTTPServer) -> None:
        barrier["server"] = server
        started.set()

    threading.Thread(
        target=serve_mode.run,
        kwargs=dict(host="127.0.0.1", port=0, enable_mcp=False, ready_callback=_ready),
        daemon=True,
    ).start()
    if not started.wait(timeout=5.0):
        raise RuntimeError("serve_mode.run never invoked ready_callback")
    server = barrier["server"]
    assert server is not None
    return server, server.server_port


def _get(url: str, timeout: float = 5.0):
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


class ServeUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dist = Path(cls.tmp.name) / "dist"
        (cls.dist / "assets").mkdir(parents=True)
        (cls.dist / "index.html").write_text(_INDEX, encoding="utf-8")
        (cls.dist / "assets" / "index-abc123.js").write_text(_JS, encoding="utf-8")
        (Path(cls.tmp.name) / "secret.txt").write_text("do not serve me", encoding="utf-8")
        cls.patch = mock.patch.object(serve_mode, "UI_DIST", cls.dist)
        cls.patch.start()
        cls.server, cls.port = _start_server()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.patch.stop()
        cls.tmp.cleanup()

    def test_root_serves_the_index(self) -> None:
        status, body, headers = _get(f"{self.base}/")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), _INDEX)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertEqual(headers["Cache-Control"], "no-cache")

    def test_hashed_asset_is_typed_and_immutable(self) -> None:
        status, body, headers = _get(f"{self.base}/assets/index-abc123.js")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), _JS)
        self.assertIn("text/javascript", headers["Content-Type"])
        self.assertIn("immutable", headers["Cache-Control"])

    def test_unknown_path_falls_back_to_the_shell(self) -> None:
        status, body, _ = _get(f"{self.base}/sessions/abc/deep/link")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), _INDEX)

    def test_missing_asset_is_a_404_not_the_shell(self) -> None:
        # Serving index.html under an image or script URL turns a bad build
        # into a rendering mystery; only extensionless paths are routes.
        for missing in ("/assets/gone-abc.js", "/engravings/absent.webp", "/x.css"):
            with self.subTest(missing=missing):
                status, body, _ = _get(f"{self.base}{missing}")
                self.assertEqual(status, 404)
                self.assertNotIn(b"<!doctype html>", body.lower())

    def test_api_keeps_precedence_over_the_app(self) -> None:
        status, body, headers = _get(f"{self.base}/v1/nope")
        self.assertEqual(status, 404)
        self.assertIn("application/json", headers["Content-Type"])
        self.assertEqual(json.loads(body)["error"]["code"], 404)

    def test_real_api_route_still_answers(self) -> None:
        status, body, _ = _get(f"{self.base}/v1/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_traversal_is_refused(self) -> None:
        for attempt in ("/../secret.txt", "/%2e%2e/secret.txt",
                        "/assets/../../secret.txt", "/..%2fsecret.txt"):
            with self.subTest(attempt=attempt):
                status, body, _ = _get(f"{self.base}{attempt}")
                self.assertIn(status, (403, 404))
                self.assertNotIn(b"do not serve me", body)


class MissingBuildTests(unittest.TestCase):
    def test_absent_dist_explains_how_to_build(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "never-built"
            with mock.patch.object(serve_mode, "UI_DIST", missing):
                server, port = _start_server()
                try:
                    status, body, _ = _get(f"http://127.0.0.1:{port}/")
                finally:
                    server.shutdown()
                    server.server_close()
        self.assertEqual(status, 503)
        self.assertIn("make ui", json.loads(body)["error"]["message"])


class AsyncTurnTests(unittest.TestCase):
    """A turn is accepted, not awaited: the response carries no result."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(serve_mode.audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(serve_mode.audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(serve_mode.audit, "META_DIR", tmp_path),
            mock.patch.object(serve_mode.audit, "LOCK_PATH", tmp_path / "audit.lock"),
            mock.patch(
                "coding_harness.tools.registry.sentinel.review",
                return_value=mock.MagicMock(allowed=True, reason="test", path="hook"),
            ),
        ]
        for p in cls.patches:
            p.start()
        cls.server, cls.port = _start_server()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def _post(self, path: str, body: dict):
        req = urllib.request.Request(
            f"{self.base}{path}", data=json.dumps(body).encode("utf-8"),
            method="POST", headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10.0) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def _session(self) -> str:
        status, body = self._post("/v1/sessions", {"autonomy": "off"})
        self.assertEqual(status, 201, body)
        return body["session_id"]

    def test_turn_is_accepted_with_a_turn_id(self) -> None:
        released = threading.Event()

        def _slow_turn(_session, _message, model_override=None, deadline_s=None):
            released.wait(timeout=10)
            return None

        sid = self._session()
        with mock.patch.object(serve_mode.Session, "run_turn", autospec=True,
                               side_effect=_slow_turn):
            status, body = self._post(f"/v1/sessions/{sid}/turn", {"message": "hi"})
            self.assertEqual(status, 202, body)
            self.assertEqual(body["status"], "accepted")
            self.assertTrue(body["turn_id"])
            # The result is not in the response — that is the whole point.
            self.assertNotIn("text", body)

            # A second turn while the first runs is refused, not queued behind
            # it, so slow turns cannot pile up blocked worker threads.
            status2, body2 = self._post(f"/v1/sessions/{sid}/turn", {"message": "again"})
            self.assertEqual(status2, 409, body2)
            self.assertIn("already running", body2["error"]["message"])
            released.set()

    def test_wait_true_still_returns_the_full_result(self) -> None:
        def _quick(_session, _message, model_override=None, deadline_s=None):
            return SessionResult(
                final_text="done", turns=1, session_id="s",
                session_log_path=Path("/tmp/s.jsonl"), halted_reason="model_done",
            )

        sid = self._session()
        with mock.patch.object(serve_mode.Session, "run_turn", autospec=True,
                               side_effect=_quick):
            status, body = self._post(
                f"/v1/sessions/{sid}/turn", {"message": "hi", "wait": True},
            )
        self.assertEqual(status, 200, body)
        self.assertEqual(body["text"], "done")

    def test_wait_must_be_a_boolean(self) -> None:
        sid = self._session()
        status, body = self._post(
            f"/v1/sessions/{sid}/turn", {"message": "hi", "wait": "yes"},
        )
        self.assertEqual(status, 400)
        self.assertIn("wait", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
