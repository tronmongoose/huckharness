"""Cross-origin gate for the serve API.

Exports ``refusal``. A loopback server with no login is still reachable from
any web page the user has open: the browser will send it a cross-site POST, and
a hostile DNS name can be rebound to 127.0.0.1. So a direct request must name a
loopback ``Host`` on a loopback bind, a state-changing request that carries an
``Origin`` must come from this server or the configured UI origin, and a body
must be declared JSON, which a cross-site form cannot do without a preflight.
Requests through a reverse proxy are left to the login gate (``serve_auth``),
whose SameSite=Strict cookie already refuses cross-site use, but only while
that gate is on: a rebound page can add a proxy header to its own requests.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from coding_harness.modes import serve_auth
from coding_harness.modes.serve_auth_req import proxied

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def refusal(handler: Any, ui_origin: str, gate: Any = None) -> tuple[int, str] | None:
    """(status, message) when the request must be refused, else None."""
    if proxied(handler) and gate is not None and not gate.disabled:
        return None
    host = (handler.headers.get("Host") or "").strip().lower()
    bind = str(handler.server.server_address[0])
    if serve_auth.is_loopback(bind) and not serve_auth.is_loopback(_hostname(host)):
        return 403, "Host header is not this server"
    if handler.command in _SAFE_METHODS:
        return None
    origin = handler.headers.get("Origin")
    if origin is not None and origin != ui_origin and urlsplit(origin).netloc.lower() != host:
        return 403, "cross-origin request refused"
    if _has_body(handler) and _content_type(handler) != "application/json":
        return 415, "request body must be application/json"
    return None


def _hostname(host: str) -> str:
    """The host part of a Host header, without port or IPv6 brackets; "" when malformed.

    Parsed by hand: before Python 3.11.4 ``urlsplit`` reads ``[::1].evil.example``
    as the bracketed address alone.
    """
    if host.startswith("["):
        end = host.find("]")
        port = host[end + 1:]
        if end == -1 or (port and not (port[0] == ":" and port[1:].isdigit())):
            return ""
        return host[1:end]
    name, sep, port = host.rpartition(":")
    if not sep:
        return host
    return name if port.isdigit() else ""


def _has_body(handler: Any) -> bool:
    """True when the request declares a non-empty body, by length or by chunking."""
    if handler.headers.get("Transfer-Encoding") is not None:
        return True
    length = (handler.headers.get("Content-Length") or "0").strip()
    return not length.isdigit() or int(length) > 0


def _content_type(handler: Any) -> str:
    """The media type of the request body, lowercased, without parameters."""
    return (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
