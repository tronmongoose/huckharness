"""Request-reading helpers for the serve login gate (``serve_auth``).

Everything here inspects one request: its path, cookie, proxy headers, the
key its failed logins count against, and a size-capped login body. Nothing
here holds state.

Exports: ``path_of``, ``is_api``, ``cookie_value``, ``is_https``,
``proxied``, ``client_key``, ``read_login_body``, ``PROXY_HEADERS``,
``MAX_LOGIN_BODY``.
"""
from __future__ import annotations

import ipaddress
import json
from typing import Any

COOKIE_NAME = "bjorn_session"
# A login body is {"token": "<43 chars>"}; anything larger is not a login.
MAX_LOGIN_BODY = 4096
# Headers a reverse proxy adds. Their presence on a loopback bind means the
# request came from the network through something like `tailscale serve`.
PROXY_HEADERS = ("X-Forwarded-For", "Forwarded", "X-Real-IP", "Tailscale-User-Login")


def path_of(handler: Any) -> str:
    """The request path without its query string."""
    return handler.path.split("?", 1)[0]


def is_api(path: str) -> bool:
    """Mirror of the dispatch rule: /v1 and below is API, the rest is the app."""
    return path == "/v1" or path.startswith("/v1/")


def cookie_value(handler: Any) -> str | None:
    """The bjorn_session cookie from the Cookie header, if present."""
    for part in (handler.headers.get("Cookie") or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == COOKIE_NAME and value:
            return value
    return None


def is_https(handler: Any) -> bool:
    """TLS on the socket, or a TLS-terminating proxy such as tailscale serve."""
    proto = (handler.headers.get("X-Forwarded-Proto") or "").split(",")[0]
    return proto.strip().lower() == "https" or hasattr(handler.connection, "getpeercert")


def proxied(handler: Any) -> bool:
    """True when any reverse-proxy header is present on the request."""
    return any(handler.headers.get(h) is not None for h in PROXY_HEADERS)


def _from_this_host(handler: Any) -> bool:
    """True when the peer is loopback or the address the server is bound to."""
    peer = handler.client_address[0]
    try:
        if ipaddress.ip_address(peer).is_loopback:
            return True
    except ValueError:
        return False
    return peer == handler.server.server_address[0]


def client_key(handler: Any) -> str:
    """What failed logins count against.

    The Tailscale login header is trusted only from this host, where the
    tailscale serve proxy runs; a direct remote client could rotate it to dodge
    the lockout, so it counts against its address instead.
    """
    user = (handler.headers.get("Tailscale-User-Login") or "").strip()
    if user and _from_this_host(handler):
        return f"ts:{user}"
    return handler.client_address[0]


def read_login_body(handler: Any) -> tuple[int, str | None]:
    """(0, token) on a readable body, else (HTTP status, None) without reading.

    Content-Length is checked before any read, so a huge or missing length
    cannot hold the handler thread on an unauthenticated route.
    """
    ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    raw_len = (handler.headers.get("Content-Length") or "").strip()
    if not raw_len.isdigit() or int(raw_len) > MAX_LOGIN_BODY:
        return 413, None
    if ctype != "application/json":
        return 415, None
    raw = handler.rfile.read(min(int(raw_len), MAX_LOGIN_BODY))
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 0, None
    token = body.get("token") if isinstance(body, dict) else None
    return 0, (token.strip() if isinstance(token, str) and token.strip() else None)
