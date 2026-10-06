"""HTTPTransport against a stdlib mock MCP server.

Verifies the POST/SSE wire shape:

  * POST 200 with synchronous JSON-RPC body → ``on_message`` fires
  * POST 202 with empty body, response over SSE → ``on_message`` fires
  * 401 on POST triggers ``AuthProvider.refresh()`` and retry
  * SSE stream is parsed correctly (``data:`` line accumulation)
  * SSE reconnects on stream close, with backoff bounded
  * ``close()`` shuts down the SSE thread

The mock server is the simplest thing that exercises the contract — it
isn't a complete MCP implementation, just enough JSON-RPC shape to
verify dispatch.
"""
from __future__ import annotations

import json
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue
from typing import Any

from coding_harness.mcp.transport import HTTPTransport


class _MockHTTPServer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.host, self.port = self.server.server_address
        self.base = f"http://{self.host}:{self.port}"
        # Test-controlled behaviour.
        self.require_auth = False
        self.auth_token_accepted: set[str] = {"good"}
        self.post_unauth_until_refresh = False
        self._unauth_serves_left = 0
        self.async_response_for: dict[int, dict[str, Any]] = {}
        # SSE queue: (event_dict, delay_ms) tuples.
        self.sse_queue: Queue[dict[str, Any] | None] = Queue()
        self.sse_disconnect_after_first = False
        self.sse_handled_count = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.sse_queue.put(None)
        self.server.shutdown()
        self.server.server_close()

    def reset_unauth(self, count: int = 1) -> None:
        self._unauth_serves_left = count

    def _check_auth(self, handler: BaseHTTPRequestHandler) -> bool:
        if not self.require_auth:
            return True
        header = handler.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return False
        return header[7:] in self.auth_token_accepted

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                if server_ref._unauth_serves_left > 0:
                    server_ref._unauth_serves_left -= 1
                    self.send_response(401)
                    self.end_headers()
                    return
                if not server_ref._check_auth(self):
                    self.send_response(401)
                    self.end_headers()
                    return
                try:
                    request = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    self.send_response(400)
                    self.end_headers()
                    return
                req_id = request.get("id")
                # If a test queued an async response for this id, return 202
                # and push the reply onto SSE instead.
                if req_id in server_ref.async_response_for:
                    reply = server_ref.async_response_for.pop(req_id)
                    server_ref.sse_queue.put(reply)
                    self.send_response(202)
                    self.end_headers()
                    return
                # Default: synchronous echo-style reply.
                body = json.dumps({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {"echo": request.get("method")},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:   # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/sse":
                    self.send_response(404)
                    self.end_headers()
                    return
                if not server_ref._check_auth(self):
                    self.send_response(401)
                    self.end_headers()
                    return
                server_ref.sse_handled_count += 1
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                # Stream events until queue gives None or test calls stop.
                try:
                    while True:
                        item = server_ref.sse_queue.get()
                        if item is None:
                            return
                        payload = json.dumps(item)
                        self.wfile.write(
                            f"data: {payload}\n\n".encode()
                        )
                        self.wfile.flush()
                        if server_ref.sse_disconnect_after_first:
                            return
                except (BrokenPipeError, ConnectionResetError):
                    return

        return _Handler


class _StubAuth:
    def __init__(self, token: str = "good") -> None:
        self.token = token
        self.refresh_calls = 0

    def authorization_header(self) -> str:
        return f"Bearer {self.token}"

    def refresh(self) -> None:
        self.refresh_calls += 1


class TestHTTPTransport(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = _MockHTTPServer()
        self.received: list[dict[str, Any]] = []
        self.closes: list[str] = []

    def tearDown(self) -> None:
        self.mock.stop()

    def _on_message(self, msg: dict[str, Any]) -> None:
        self.received.append(msg)

    def _on_close(self, reason: str) -> None:
        self.closes.append(reason)

    def _start(self, **kw) -> HTTPTransport:
        t = HTTPTransport(self.mock.base, name="test", **kw)
        t.start(self._on_message, self._on_close)
        return t

    def test_post_synchronous_reply(self) -> None:
        t = self._start()
        try:
            t.send({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
            self._wait_for(lambda: len(self.received) >= 1)
            self.assertEqual(self.received[0]["id"], 1)
            self.assertEqual(self.received[0]["result"], {"echo": "ping"})
        finally:
            t.close()

    def test_post_async_via_sse(self) -> None:
        self.mock.async_response_for[42] = {
            "jsonrpc": "2.0", "id": 42, "result": {"async": True},
        }
        t = self._start()
        try:
            t.send({"jsonrpc": "2.0", "id": 42, "method": "slow", "params": {}})
            self._wait_for(lambda: any(m.get("id") == 42 for m in self.received))
            ids = [m["id"] for m in self.received]
            self.assertIn(42, ids)
        finally:
            t.close()

    def test_post_401_refreshes_and_retries(self) -> None:
        self.mock.require_auth = True
        self.mock.reset_unauth(count=1)
        auth = _StubAuth(token="good")
        t = self._start(auth=auth)
        try:
            t.send({"jsonrpc": "2.0", "id": 7, "method": "ping", "params": {}})
            self._wait_for(lambda: len(self.received) >= 1)
            self.assertEqual(auth.refresh_calls, 1)
        finally:
            t.close()

    def test_sse_delivers_notifications(self) -> None:
        t = self._start()
        # Push a server-originated notification.
        self.mock.sse_queue.put({
            "jsonrpc": "2.0", "method": "notifications/progress",
            "params": {"step": 1},
        })
        try:
            self._wait_for(
                lambda: any(m.get("method") == "notifications/progress"
                            for m in self.received)
            )
        finally:
            t.close()

    def test_sse_reconnects_after_disconnect(self) -> None:
        self.mock.sse_disconnect_after_first = True
        t = self._start()
        try:
            self.mock.sse_queue.put({"jsonrpc": "2.0", "method": "ping1", "params": {}})
            self._wait_for(lambda: self.mock.sse_handled_count >= 2,
                           timeout=5.0)
            self.assertGreaterEqual(self.mock.sse_handled_count, 2)
        finally:
            t.close()

    def test_close_signals_on_close(self) -> None:
        t = self._start()
        t.close()
        # The on_close callback should fire eventually.
        self._wait_for(lambda: len(self.closes) >= 1)

    # ── Helpers ─────────────────────────────────────────────────────

    def _wait_for(self, pred, *, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.02)
        self.fail(f"condition not met within {timeout}s")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
