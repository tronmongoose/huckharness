"""Login gate for ``bjorn serve`` when the network can reach it.

A plain loopback request stays open: only local processes make one. A
non-loopback bind, or a loopback request carrying reverse-proxy headers
(``tailscale serve 9100``), exposes session turns, permission approvals and
project spawning to the network, so every ``/v1`` route then needs a session
cookie issued by ``POST /v1/auth`` for the token in ``~/.config/bjorn/auth_token``.

The check runs once per request, before any route dispatch, so a route added
later is covered without touching this module. Static UI assets and the SPA
shell stay open because the app itself renders the login screen. The token is
never logged, printed or accepted in a query string.

Exports: ``AuthGate`` (per-server state and the request hook), ``is_loopback``,
``token_path``, ``token_path_hint``, ``ensure_token``, ``startup_lines``, ``COOKIE_NAME``.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

from coding_harness.core.settings import USER_SETTINGS
from coding_harness.modes import serve_auth_req as req
from coding_harness.modes.serve_auth_req import COOKIE_NAME

SESSION_TTL_S = 30 * 24 * 3600
MAX_SESSIONS = 256
MAX_FAILURES = 5
LOCKOUT_S = 60.0
# Bounds the failure table so a scan from many addresses cannot grow it forever.
MAX_TRACKED_CLIENTS = 1024
LOGIN_PATH = "/v1/auth"
LOGOUT_PATH = "/v1/auth/logout"
STATUS_PATH = "/v1/auth/status"
_LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


def is_loopback(host: str) -> bool:
    """True when ``host`` can only be reached from this machine."""
    name = host.strip().strip("[]").lower()
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def auth_disabled() -> bool:
    """HARNESS_AUTH=0 turns the gate off even on a non-loopback bind."""
    return os.environ.get("HARNESS_AUTH", "1").strip() == "0"


def token_path() -> Path:
    """The token file: HARNESS_AUTH_TOKEN_FILE, else next to settings.json."""
    override = os.environ.get("HARNESS_AUTH_TOKEN_FILE")
    if override:
        return Path(os.path.expanduser(override))
    return Path(os.path.expanduser(str(USER_SETTINGS))).parent / "auth_token"


def token_path_hint(path: Path) -> str:
    """``<parent>/<name>`` of the token file: enough to find it, never the home layout."""
    return f"{path.parent.name}/{path.name}" if path.parent.name else path.name


def ensure_token(path: Path) -> bool:
    """Create the token file with mode 0600 if absent. True when created."""
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(secrets.token_urlsafe(32) + "\n")
    os.chmod(str(path), 0o600)
    return True


def _read_token(path: Path) -> str | None:
    """The stripped token, or None when the file is missing or empty."""
    try:
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def startup_lines(host: str) -> list[str]:
    """Lines ``bjorn serve`` prints for this bind. Never includes the token."""
    if is_loopback(host):
        return []
    if auth_disabled():
        return [f"WARNING: HARNESS_AUTH=0 and bound to {host}: every API route, "
                "including running turns and approving permissions, is open "
                "to anyone who can reach this address."]
    path = token_path()
    created = ensure_token(path)
    lines = [f"GUI login token is in {path}; paste it into the login screen."]
    if created:
        lines.insert(0, f"Generated a new GUI login token (mode 0600) at {path}.")
    return lines


def _log(event: str, client: str) -> None:
    """One stderr line per auth event, matching the serve request log."""
    sys.stderr.write(f"[serve-auth] {client} - {event}\n")



class AuthGate:
    """Per-server auth state: token path, live session ids, failed logins."""

    def __init__(self, host: str) -> None:
        """Remember the bind; a non-loopback one needs the token file now."""
        self.disabled = auth_disabled()
        self.remote_bind = not is_loopback(host)
        self.path = token_path()
        if self.remote_bind and not self.disabled:
            ensure_token(self.path)
        self._lock = threading.Lock()
        self._sessions: dict[str, float] = {}
        self._failures: dict[str, list[float]] = {}

    def required_for(self, handler: Any) -> bool:
        """Whether this request must carry a session.

        A loopback bind is still reachable from the network through a local
        reverse proxy (`tailscale serve 9100`), so a request with proxy
        headers is treated as remote. Plain loopback requests stay open.
        """
        if self.disabled:
            return False
        if self.remote_bind:
            return True
        if not req.proxied(handler):
            return False
        # The token file is created on the first proxied request, not at a
        # loopback start, so `bjorn gui` and the tests never write it.
        if ensure_token(self.path):
            _log(f"proxied request, new GUI login token is in {self.path}", "-")
        return True

    # ── request hook ─────────────────────────────────────────────

    def intercept(self, handler: Any) -> bool:
        """Answer auth routes and refuse unauthenticated API calls.

        True when this method sent the response, so dispatch must stop.
        """
        path, method = req.path_of(handler), handler.command
        if path == STATUS_PATH and method == "GET":
            self._status(handler)
            return True
        if path == LOGIN_PATH and method == "POST":
            self._login(handler)
            return True
        if self._blocked(handler, path, method):
            _refuse(handler, 401, {"error": "unauthorized"})
            return True
        if path == LOGOUT_PATH and method == "POST":
            self._logout(handler)
            return True
        return False

    def _blocked(self, handler: Any, path: str, method: str) -> bool:
        """True for an API request that needs a session and lacks one."""
        if method == "OPTIONS" or not req.is_api(path):
            return False
        return self.required_for(handler) and not self.valid(req.cookie_value(handler))

    # ── sessions ─────────────────────────────────────────────────

    def valid(self, sid: str | None) -> bool:
        """True when ``sid`` names a live, unexpired session.

        Compares against every stored id with compare_digest rather than a
        dict lookup, so timing says nothing about how much of an id matched.
        The table is capped at MAX_SESSIONS, which bounds the scan.
        """
        if not sid:
            return False
        now = time.time()
        with self._lock:
            match = None
            for known in self._sessions:
                if hmac.compare_digest(known.encode(), sid.encode()):
                    match = known
            if match is None:
                return False
            if self._sessions[match] <= now:
                del self._sessions[match]
                return False
            return True

    def _new_session(self) -> str:
        """Mint a session id, pruning expired and, at the cap, the oldest."""
        sid = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            for old in [k for k, exp in self._sessions.items() if exp <= now]:
                del self._sessions[old]
            if len(self._sessions) >= MAX_SESSIONS:
                oldest = min(self._sessions, key=self._sessions.__getitem__)
                del self._sessions[oldest]
            self._sessions[sid] = now + SESSION_TTL_S
        return sid

    # ── rate limit ───────────────────────────────────────────────

    def _locked_for(self, client: str) -> float:
        """Seconds left on this client's lockout, 0 when not locked."""
        with self._lock:
            entry = self._failures.get(client)
            if entry is None:
                return 0.0
            return max(0.0, entry[1] - time.time())

    def _record_failure(self, client: str) -> int:
        """Count a failed login, locking the client at MAX_FAILURES. Attempts left."""
        now = time.time()
        with self._lock:
            if client not in self._failures and len(self._failures) >= MAX_TRACKED_CLIENTS:
                for old in [c for c, e in self._failures.items() if e[1] <= now]:
                    del self._failures[old]
            entry = self._failures.setdefault(client, [0.0, 0.0])
            if entry[1] and entry[1] <= now:
                entry[0], entry[1] = 0.0, 0.0
            entry[0] += 1
            if entry[0] >= MAX_FAILURES:
                entry[0], entry[1] = 0.0, now + LOCKOUT_S
                return 0
            return int(MAX_FAILURES - entry[0])

    # ── routes ───────────────────────────────────────────────────

    def _status(self, handler: Any) -> None:
        """GET /v1/auth/status: whether this request needs a login and has one."""
        required = self.required_for(handler)
        authed = not required or self.valid(req.cookie_value(handler))
        body: dict[str, Any] = {"required": required, "authenticated": authed}
        if required:
            body["token_path_hint"] = token_path_hint(self.path)
        handler._send_json(200, body)

    def _login(self, handler: Any) -> None:
        """POST /v1/auth {"token"}: trade the token for a session cookie."""
        client = req.client_key(handler)
        status, token = req.read_login_body(handler)
        if status:
            _refuse(handler, status, {"error": "bad login request"})
            return
        if not self.required_for(handler):
            handler._send_json(200, {"ok": True, "required": False})
            return
        wait = self._locked_for(client)
        if wait > 0:
            _log("login refused, locked out", client)
            handler._send_json(429, {"error": "locked", "retry_after": int(wait) + 1},
                               extra_headers={"Retry-After": str(int(wait) + 1)})
            return
        expected = _read_token(self.path)
        if expected is None:
            _log("login refused, token file missing", client)
            handler._send_json(503, {"error": "token file missing"})
            return
        if token is None or not hmac.compare_digest(token.encode(), expected.encode()):
            left = self._record_failure(client)
            _log(f"login failed, {left} attempts left", client)
            handler._send_json(401, {"error": "unauthorized", "attempts_left": left})
            return
        with self._lock:
            self._failures.pop(client, None)
        _log("login ok", client)
        handler._send_json(200, {"ok": True, "required": True},
                           extra_headers={"Set-Cookie": self._cookie(handler)})

    def _cookie(self, handler: Any) -> str:
        """The Set-Cookie value for a fresh session."""
        cookie = (f"{COOKIE_NAME}={self._new_session()}; HttpOnly; SameSite=Strict; "
                  f"Path=/; Max-Age={SESSION_TTL_S}")
        return cookie + "; Secure" if req.is_https(handler) else cookie

    def _logout(self, handler: Any) -> None:
        """POST /v1/auth/logout: forget this session and expire the cookie."""
        sid = req.cookie_value(handler)
        with self._lock:
            self._sessions.pop(sid or "", None)
        _log("logout", handler.client_address[0])
        clear = f"{COOKIE_NAME}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
        handler._send_json(200, {"ok": True}, extra_headers={"Set-Cookie": clear})


def _refuse(handler: Any, status: int, payload: dict[str, Any]) -> None:
    """Answer and close. The body was never read, so it must not be parsed
    as the next request on a kept-alive connection."""
    handler.close_connection = True
    handler._send_json(status, payload, extra_headers={"Connection": "close"})
