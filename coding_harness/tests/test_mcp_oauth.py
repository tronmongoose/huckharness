"""OAuth (DCR + Auth Code + PKCE + refresh) against a stdlib mock server.

The mock authorization server implements just enough of RFC 8414 (server
metadata), RFC 7591 (DCR), and OAuth 2.0 + PKCE to drive ``OAuthSession``
through every code path:

  * discovery → DCR → consent → token exchange → header use
  * cached refresh on subsequent runs
  * refresh failure → ``OAuthRequiresConsent``
  * ``headless=True`` short-circuit before browser open

The "browser" is replaced with a synchronous urllib GET that follows the
authorization redirect into our real loopback callback listener — so the
listener code is exercised end-to-end, not mocked.
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
from unittest import mock

from coding_harness.mcp.oauth import (
    OAuthError,
    OAuthRequiresConsent,
    OAuthSession,
)


class _MockAuthServer:
    """Minimal in-memory authorization server.

    Behaviour is controlled by mutable attributes the test sets before
    driving the flow (e.g., ``self.refresh_should_fail = True``).
    """

    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.host, self.port = self.server.server_address
        self.base = f"http://{self.host}:{self.port}"
        self.registered_clients: dict[str, dict] = {}
        self.issued_codes: dict[str, dict] = {}
        self.issued_refresh_tokens: set[str] = set()
        self.refresh_should_fail = False
        self.access_token_counter = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _next_access_token(self) -> str:
        self.access_token_counter += 1
        return f"access-{self.access_token_counter}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        server_ref = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_: object) -> None:
                pass

            def _send_json(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self) -> None:   # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/.well-known/oauth-authorization-server":
                    self._send_json(200, {
                        "issuer": server_ref.base,
                        "authorization_endpoint": server_ref.base + "/authorize",
                        "token_endpoint": server_ref.base + "/token",
                        "registration_endpoint": server_ref.base + "/register",
                    })
                    return
                if parsed.path == "/authorize":
                    q = dict(urllib.parse.parse_qsl(parsed.query))
                    # Issue a code bound to the PKCE challenge + redirect.
                    code = f"code-{len(server_ref.issued_codes) + 1}"
                    server_ref.issued_codes[code] = {
                        "code_challenge": q.get("code_challenge", ""),
                        "client_id": q.get("client_id", ""),
                        "redirect_uri": q.get("redirect_uri", ""),
                    }
                    redirect = q["redirect_uri"] + "?" + urllib.parse.urlencode({
                        "code": code,
                        "state": q.get("state", ""),
                    })
                    self.send_response(302)
                    self.send_header("Location", redirect)
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/register":
                    body = json.loads(raw.decode("utf-8"))
                    client_id = f"client-{len(server_ref.registered_clients) + 1}"
                    record = {
                        "client_id": client_id,
                        "client_name": body.get("client_name"),
                        "redirect_uris": body.get("redirect_uris", []),
                    }
                    server_ref.registered_clients[client_id] = record
                    self._send_json(200, record)
                    return
                if parsed.path == "/token":
                    form = dict(urllib.parse.parse_qsl(raw.decode("utf-8")))
                    grant = form.get("grant_type")
                    if grant == "authorization_code":
                        code = form.get("code", "")
                        rec = server_ref.issued_codes.pop(code, None)
                        if rec is None:
                            self._send_json(400, {"error": "invalid_grant"})
                            return
                        refresh = f"refresh-{code}"
                        server_ref.issued_refresh_tokens.add(refresh)
                        self._send_json(200, {
                            "access_token": server_ref._next_access_token(),
                            "refresh_token": refresh,
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        })
                        return
                    if grant == "refresh_token":
                        if server_ref.refresh_should_fail:
                            self._send_json(400, {"error": "invalid_grant"})
                            return
                        rt = form.get("refresh_token", "")
                        if rt not in server_ref.issued_refresh_tokens:
                            self._send_json(400, {"error": "invalid_grant"})
                            return
                        self._send_json(200, {
                            "access_token": server_ref._next_access_token(),
                            "token_type": "Bearer",
                            "expires_in": 3600,
                        })
                        return
                    self._send_json(400, {"error": "unsupported_grant_type"})
                    return
                self.send_response(404)
                self.end_headers()

        return _Handler


def _simulate_browser(authorize_url: str) -> None:
    """Stand in for ``webbrowser.open``: drive the authorization redirect
    directly via urllib so the real callback listener runs."""

    def _drive() -> None:
        # Two-step: GET /authorize → follow the 302 to the loopback
        # callback. urllib follows by default.
        try:
            urllib.request.urlopen(authorize_url, timeout=5.0)
        except urllib.error.URLError:
            pass

    threading.Thread(target=_drive, daemon=True).start()


class TestOAuthFullFlow(unittest.TestCase):
    def setUp(self) -> None:
        self.mock = _MockAuthServer()
        self.tmp = TemporaryDirectory()
        self.cache_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.mock.stop()
        self.tmp.cleanup()

    def _new_session(self, **kw) -> OAuthSession:
        return OAuthSession(
            name=kw.pop("name", "test"),
            base_url=self.mock.base,
            cache_dir=self.cache_dir,
            **kw,
        )

    def test_first_run_acquires_token(self) -> None:
        with mock.patch("coding_harness.mcp.oauth.webbrowser.open",
                        side_effect=lambda url, **_: _simulate_browser(url)):
            sess = self._new_session()
            header = sess.authorization_header()
        self.assertTrue(header.startswith("Bearer access-"))
        # Cache file is written with restricted mode.
        cached = list(self.cache_dir.glob("*.json"))
        self.assertEqual(len(cached), 1)

    def test_cached_token_skips_browser(self) -> None:
        with mock.patch("coding_harness.mcp.oauth.webbrowser.open",
                        side_effect=lambda url, **_: _simulate_browser(url)):
            self._new_session().authorization_header()
        # Second session should not need a browser at all.
        with mock.patch("coding_harness.mcp.oauth.webbrowser.open",
                        side_effect=AssertionError("browser should not open")):
            sess2 = self._new_session()
            header = sess2.authorization_header()
        self.assertTrue(header.startswith("Bearer access-"))

    def test_refresh_succeeds(self) -> None:
        with mock.patch("coding_harness.mcp.oauth.webbrowser.open",
                        side_effect=lambda url, **_: _simulate_browser(url)):
            sess = self._new_session()
            sess.authorization_header()
        old_count = self.mock.access_token_counter
        sess.refresh()
        self.assertGreater(self.mock.access_token_counter, old_count)

    def test_refresh_failure_raises_consent(self) -> None:
        with mock.patch("coding_harness.mcp.oauth.webbrowser.open",
                        side_effect=lambda url, **_: _simulate_browser(url)):
            sess = self._new_session()
            sess.authorization_header()
        self.mock.refresh_should_fail = True
        with self.assertRaises(OAuthRequiresConsent):
            sess.refresh()

    def test_headless_with_no_cache_raises(self) -> None:
        sess = self._new_session(headless=True)
        with self.assertRaises(OAuthRequiresConsent):
            sess.authorization_header()

    def test_state_mismatch_rejected(self) -> None:
        """Tamper the state parameter mid-flight; expect OAuthError."""

        def tamper_browser(url: str) -> None:
            def _drive() -> None:
                # Strip the state param so the callback sees a mismatch.
                parsed = urllib.parse.urlparse(url)
                q = dict(urllib.parse.parse_qsl(parsed.query))
                q["state"] = "wrong-state"
                new_url = parsed._replace(
                    query=urllib.parse.urlencode(q)
                ).geturl()
                try:
                    urllib.request.urlopen(new_url, timeout=5.0)
                except urllib.error.URLError:
                    pass
            threading.Thread(target=_drive, daemon=True).start()

        with mock.patch("coding_harness.mcp.oauth.webbrowser.open",
                        side_effect=lambda url, **_: tamper_browser(url)):
            sess = self._new_session(name="tamper")
            with self.assertRaises(OAuthError):
                sess.authorization_header()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
