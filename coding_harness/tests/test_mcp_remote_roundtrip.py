"""End-to-end: MCPClient over HTTP + OAuth against composed mock servers.

Composes a mock authorization server and a mock MCP HTTP server in one
test process, then drives the real :class:`MCPClient` through the full
lifecycle: ``start`` → ``list_tools`` → ``call_tool`` → ``close``. The
client doesn't know it's not talking to a real Anthropic-hosted server.

This is the smoke test that proves the transport refactor and the OAuth
state machine line up — the unit-level files exercise their pieces in
isolation; this one exercises the composition.
"""
from __future__ import annotations

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest import mock

from coding_harness.mcp.client import MCPClient
from coding_harness.mcp.config import MCPServerConfig
from coding_harness.mcp.oauth import OAuthSession
from coding_harness.mcp.transport import HTTPTransport


class _MockAuthServer:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.host, self.port = self.server.server_address
        self.base = f"http://{self.host}:{self.port}"
        self._access_n = 0
        self._refresh_n = 0
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        ref = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def _json(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:   # noqa: N802
                p = urllib.parse.urlparse(self.path)
                if p.path == "/.well-known/oauth-authorization-server":
                    self._json(200, {
                        "issuer": ref.base,
                        "authorization_endpoint": ref.base + "/authorize",
                        "token_endpoint": ref.base + "/token",
                        "registration_endpoint": ref.base + "/register",
                    })
                    return
                if p.path == "/authorize":
                    q = dict(urllib.parse.parse_qsl(p.query))
                    redirect = q["redirect_uri"] + "?" + urllib.parse.urlencode({
                        "code": "the-code", "state": q.get("state", ""),
                    })
                    self.send_response(302)
                    self.send_header("Location", redirect)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                _ = self.rfile.read(length) if length else b""
                p = urllib.parse.urlparse(self.path)
                if p.path == "/register":
                    self._json(200, {"client_id": "client-xyz"})
                    return
                if p.path == "/token":
                    ref._access_n += 1
                    self._json(200, {
                        "access_token": f"tok-{ref._access_n}",
                        "refresh_token": "refresh-xyz",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                    })
                    return
                self.send_response(404)
                self.end_headers()

        return _H


class _MockMCPServer:
    """Tiny MCP-over-HTTP server: handles initialize, tools/list, tools/call."""

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.host, self.port = self.server.server_address
        self.base = f"http://{self.host}:{self.port}"
        self.require_token: str | None = None
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        ref = self

        class _H(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def _auth_ok(self) -> bool:
                if ref.require_token is None:
                    return True
                hdr = self.headers.get("Authorization", "")
                return hdr == f"Bearer {ref.require_token}"

            def do_GET(self) -> None:   # noqa: N802
                if urllib.parse.urlparse(self.path).path != "/sse":
                    self.send_response(404)
                    self.end_headers()
                    return
                if not self._auth_ok():
                    self.send_response(401)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                # Block until client disconnects.
                try:
                    while True:
                        if self.wfile.closed:
                            return
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        import time as _t
                        _t.sleep(0.5)
                except (BrokenPipeError, ConnectionResetError):
                    return

            def do_POST(self) -> None:  # noqa: N802
                if not self._auth_ok():
                    self.send_response(401)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                req = json.loads(raw.decode("utf-8"))
                method = req.get("method", "")
                req_id = req.get("id")
                if method == "initialize":
                    result: dict[str, Any] = {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "serverInfo": {"name": "mock", "version": "0"},
                    }
                elif method == "tools/list":
                    result = {"tools": [{
                        "name": "echo",
                        "description": "Echo input back",
                        "inputSchema": {"type": "object"},
                    }]}
                elif method == "tools/call":
                    args = req.get("params", {}).get("arguments", {})
                    result = {
                        "content": [{"type": "text", "text": json.dumps(args)}],
                        "isError": False,
                    }
                elif req_id is None:
                    # Notification (e.g. notifications/initialized). 202 + no body.
                    self.send_response(202)
                    self.end_headers()
                    return
                else:
                    self.send_response(400)
                    self.end_headers()
                    return
                body = json.dumps({
                    "jsonrpc": "2.0", "id": req_id, "result": result,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _H


def _simulate_browser(url: str) -> None:
    def _drive() -> None:
        try:
            urllib.request.urlopen(url, timeout=5.0)
        except urllib.error.URLError:
            pass
    threading.Thread(target=_drive, daemon=True).start()


class TestRemoteRoundtrip(unittest.TestCase):
    def setUp(self) -> None:
        self.auth = _MockAuthServer()
        self.mcp = _MockMCPServer()
        self.tmp = TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.mcp.stop()
        self.auth.stop()
        self.tmp.cleanup()

    def test_initialize_list_call_close(self) -> None:
        self.mcp.require_token = "tok-1"
        oauth = OAuthSession(
            name="mock", base_url=self.auth.base, cache_dir=self.cache_dir,
        )
        transport = HTTPTransport(self.mcp.base, name="mock", auth=oauth)
        cfg = MCPServerConfig(name="mock", url=self.mcp.base, oauth=True)
        client = MCPClient(cfg, transport=transport)
        with mock.patch("coding_harness.mcp.oauth.webbrowser.open",
                        side_effect=lambda url, **_: _simulate_browser(url)):
            client.start()
        try:
            tools = client.list_tools()
            self.assertEqual([t["name"] for t in tools], ["echo"])
            result = client.call_tool("echo", {"hello": "world"})
            self.assertFalse(result.get("isError"))
            self.assertIn("world", result["content"][0]["text"])
        finally:
            client.close()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
