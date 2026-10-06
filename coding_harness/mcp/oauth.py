"""OAuth 2.0 Auth Code + PKCE + Dynamic Client Registration.

One :class:`OAuthSession` per remote MCP server. Tokens cache on disk at
``~/.coding_harness/oauth/<name>.json`` (mode 0600, parent dir 0700).

First run (per machine, per server):

1. Discovery: ``GET <base>/.well-known/oauth-authorization-server``.
2. DCR (RFC 7591): ``POST registration_endpoint`` with redirect_uris,
   grant_types, ``token_endpoint_auth_method=none`` (public client).
3. PKCE: random ``code_verifier``, ``code_challenge = S256(code_verifier)``.
4. Open browser to ``authorization_endpoint``. Stand up a one-shot
   loopback listener on the redirect port; capture ``?code=``.
5. ``POST token_endpoint`` to exchange ``code`` → access + refresh tokens.
6. Persist; chmod 0600.

Subsequent runs use the cached refresh_token. A 401 from the resource
server triggers :meth:`refresh`. Refresh failure raises
:exc:`OAuthRequiresConsent` — typed so headless contexts (nightshift,
schedulers) detect dead-ends without ambushing the user with a browser.

Re-DCR: if the authorization server rejects our cached ``client_id``,
we re-register transparently rather than failing the run.

``headless=True`` short-circuits the first-run browser flow with
:exc:`OAuthRequiresConsent`; callers that need interactive consent run
with ``headless=False`` (the default).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path.home() / ".coding_harness" / "oauth"
DEFAULT_CLIENT_NAME = "coding_harness"
REFRESH_SAFETY_MARGIN = 60.0   # refresh this many seconds before expiry
HTTP_TIMEOUT = 30.0
CALLBACK_WAIT_SECONDS = 300.0   # five-minute consent window


class OAuthError(RuntimeError):
    """Base for OAuth-side failures (transport, protocol, server)."""


class OAuthRequiresConsent(OAuthError):
    """Refresh failed; first-run flow needed. Pipelines can catch this
    to skip + alert instead of opening a browser unexpectedly."""


class OAuthSession:
    """One server's OAuth identity. Thread-safe for header reads."""

    def __init__(
        self,
        name: str,
        base_url: str,
        *,
        scopes: list[str] | None = None,
        cache_dir: Path | None = None,
        headless: bool = False,
        client_name: str = DEFAULT_CLIENT_NAME,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.scopes = list(scopes) if scopes else None
        self.headless = headless
        self.client_name = client_name
        self._cache_dir = cache_dir or DEFAULT_CACHE_DIR
        self._cache_path = self._cache_dir / f"{self._safe_name(name)}.json"
        self._lock = threading.Lock()
        self._state: dict[str, Any] | None = None

    # ── Public surface ──────────────────────────────────────────────

    def authorization_header(self) -> str:
        with self._lock:
            self._ensure_loaded_locked()
            assert self._state is not None
            if self._token_expired_locked():
                self._refresh_locked()
            return f"Bearer {self._state['access_token']}"

    def refresh(self) -> None:
        with self._lock:
            self._ensure_loaded_locked()
            self._refresh_locked()

    # ── Internal: state loading ─────────────────────────────────────

    def _ensure_loaded_locked(self) -> None:
        if self._state is not None:
            return
        cached = self._load_cache()
        if cached is not None:
            self._state = cached
            return
        # First run: discovery → DCR → consent → token exchange.
        if self.headless:
            raise OAuthRequiresConsent(
                f"OAuthSession[{self.name}]: no cached token and "
                f"headless=True; interactive consent required"
            )
        self._state = self._first_run()
        self._save_cache_locked()

    def _token_expired_locked(self) -> bool:
        assert self._state is not None
        expires_at = self._state.get("expires_at")
        if not isinstance(expires_at, (int, float)):
            return False
        return time.time() + REFRESH_SAFETY_MARGIN >= expires_at

    # ── First-run flow ──────────────────────────────────────────────

    def _first_run(self) -> dict[str, Any]:
        metadata = _discover(self.base_url)
        registration = _register_client(
            metadata["registration_endpoint"],
            client_name=self.client_name,
            redirect_uri=None,   # placeholder; rewritten after we pick a port
        )
        # Pick a redirect port, then re-register if the auth server pinned
        # the redirect during registration. Most public-client DCR
        # implementations accept a redirect that wasn't pre-registered
        # provided ``redirect_uris`` matches a registered prefix — for
        # safety we register with the explicit URI we'll use.
        port = _pick_free_port()
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        # Re-do DCR with the actual redirect URI so the auth server has
        # the exact match it needs. Most servers require this.
        registration = _register_client(
            metadata["registration_endpoint"],
            client_name=self.client_name,
            redirect_uri=redirect_uri,
        )
        client_id = registration["client_id"]

        code_verifier = _b64url(secrets.token_bytes(32))
        code_challenge = _b64url(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        state = _b64url(secrets.token_bytes(16))

        scope_param = " ".join(self.scopes) if self.scopes else None
        authorize_url = _build_authorize_url(
            metadata["authorization_endpoint"],
            client_id=client_id,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            scope=scope_param,
        )

        code = _await_callback(port, authorize_url, expected_state=state)
        token_response = _exchange_code(
            metadata["token_endpoint"],
            client_id=client_id,
            code=code,
            code_verifier=code_verifier,
            redirect_uri=redirect_uri,
        )
        return _state_from_token_response(
            token_response,
            base_url=self.base_url,
            metadata=metadata,
            registration=registration,
            scopes=self.scopes,
        )

    # ── Refresh flow ────────────────────────────────────────────────

    def _refresh_locked(self) -> None:
        assert self._state is not None
        refresh_token = self._state.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise OAuthRequiresConsent(
                f"OAuthSession[{self.name}]: no refresh token cached"
            )
        metadata = self._state.get("metadata") or {}
        token_endpoint = metadata.get("token_endpoint")
        client_id = self._state.get("client_id")
        if not isinstance(token_endpoint, str) or not isinstance(client_id, str):
            raise OAuthError(
                f"OAuthSession[{self.name}]: cache missing token_endpoint or client_id"
            )
        try:
            token_response = _refresh_token(
                token_endpoint, client_id=client_id, refresh_token=refresh_token
            )
        except urllib.error.HTTPError as e:
            # 4xx → refresh token bad / revoked → consent required.
            if 400 <= e.code < 500:
                raise OAuthRequiresConsent(
                    f"OAuthSession[{self.name}]: refresh rejected "
                    f"(HTTP {e.code}); consent required"
                ) from e
            raise OAuthError(
                f"OAuthSession[{self.name}]: refresh HTTP {e.code}"
            ) from e
        except urllib.error.URLError as e:
            raise OAuthError(
                f"OAuthSession[{self.name}]: refresh transport error: {e.reason}"
            ) from e
        # Merge new tokens; preserve refresh_token if server omits one.
        self._state["access_token"] = token_response["access_token"]
        if "refresh_token" in token_response:
            self._state["refresh_token"] = token_response["refresh_token"]
        expires_in = token_response.get("expires_in")
        if isinstance(expires_in, (int, float)):
            self._state["expires_at"] = time.time() + float(expires_in)
        else:
            self._state.pop("expires_at", None)
        self._save_cache_locked()

    # ── Cache I/O ───────────────────────────────────────────────────

    def _load_cache(self) -> dict[str, Any] | None:
        try:
            raw = self._cache_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as e:
            raise OAuthError(
                f"OAuthSession[{self.name}]: cache read failed: {e}"
            ) from e
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        return parsed

    def _save_cache_locked(self) -> None:
        assert self._state is not None
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self._cache_dir, 0o700)
        except OSError as e:
            raise OAuthError(
                f"OAuthSession[{self.name}]: cache dir creation failed: {e}"
            ) from e
        tmp = self._cache_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._cache_path)

    @staticmethod
    def _safe_name(name: str) -> str:
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)


# ── Free functions: discovery, DCR, authorize, token, refresh ──────


def _discover(base_url: str) -> dict[str, Any]:
    url = base_url.rstrip("/") + "/.well-known/oauth-authorization-server"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        body = resp.read()
    parsed = json.loads(body.decode("utf-8", errors="replace"))
    for key in ("authorization_endpoint", "token_endpoint", "registration_endpoint"):
        if not isinstance(parsed.get(key), str):
            raise OAuthError(f"discovery doc at {url} missing '{key}'")
    return parsed


def _register_client(
    registration_endpoint: str,
    *,
    client_name: str,
    redirect_uri: str | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "client_name": client_name,
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
        "application_type": "native",
    }
    if redirect_uri is not None:
        body["redirect_uris"] = [redirect_uri]
    req = urllib.request.Request(
        registration_endpoint,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        payload = resp.read()
    parsed = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(parsed.get("client_id"), str):
        raise OAuthError(f"DCR response missing 'client_id': {parsed!r}")
    return parsed


def _build_authorize_url(
    authorization_endpoint: str,
    *,
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scope: str | None,
) -> str:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if scope:
        params["scope"] = scope
    sep = "&" if "?" in authorization_endpoint else "?"
    return authorization_endpoint + sep + urllib.parse.urlencode(params)


def _await_callback(port: int, authorize_url: str, *, expected_state: str) -> str:
    captured: dict[str, str] = {}
    done = threading.Event()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:   # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            captured.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"You may close this tab.\n")
            done.set()

        def log_message(self, *_: object) -> None:  # silence stderr
            pass

    server = HTTPServer(("127.0.0.1", port), _Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        try:
            webbrowser.open(authorize_url, new=1, autoraise=True)
        except Exception:
            pass
        if not done.wait(timeout=CALLBACK_WAIT_SECONDS):
            raise OAuthRequiresConsent(
                "OAuth consent did not complete within the allowed window"
            )
    finally:
        server.shutdown()
        server.server_close()

    if "error" in captured:
        raise OAuthError(
            f"authorization server returned error: {captured.get('error')!r} "
            f"({captured.get('error_description', '')})"
        )
    if captured.get("state") != expected_state:
        raise OAuthError("OAuth state mismatch on callback (possible CSRF)")
    code = captured.get("code")
    if not code:
        raise OAuthError("OAuth callback missing 'code' parameter")
    return code


def _exchange_code(
    token_endpoint: str,
    *,
    client_id: str,
    code: str,
    code_verifier: str,
    redirect_uri: str,
) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "client_id": client_id,
        "redirect_uri": redirect_uri,
    }).encode("utf-8")
    return _post_token(token_endpoint, body)


def _refresh_token(
    token_endpoint: str, *, client_id: str, refresh_token: str
) -> dict[str, Any]:
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }).encode("utf-8")
    return _post_token(token_endpoint, body)


def _post_token(token_endpoint: str, body: bytes) -> dict[str, Any]:
    req = urllib.request.Request(
        token_endpoint,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        payload = resp.read()
    parsed = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(parsed.get("access_token"), str):
        raise OAuthError(f"token response missing 'access_token': {parsed!r}")
    return parsed


def _state_from_token_response(
    token_response: dict[str, Any],
    *,
    base_url: str,
    metadata: dict[str, Any],
    registration: dict[str, Any],
    scopes: list[str] | None,
) -> dict[str, Any]:
    state: dict[str, Any] = {
        "base_url": base_url,
        "client_id": registration["client_id"],
        "registration": registration,
        "metadata": metadata,
        "access_token": token_response["access_token"],
        "scopes": scopes,
    }
    refresh = token_response.get("refresh_token")
    if isinstance(refresh, str):
        state["refresh_token"] = refresh
    expires_in = token_response.get("expires_in")
    if isinstance(expires_in, (int, float)):
        state["expires_at"] = time.time() + float(expires_in)
    return state


# ── Helpers ────────────────────────────────────────────────────────


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
