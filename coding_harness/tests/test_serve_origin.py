"""The cross-origin and DNS-rebinding gate on the serve API, against a real loopback server.

Requests go over a raw socket so every header is the test's own. ``/v1/nope``
is an unknown route: a request the gate lets through answers 404, a refused one
answers 403 or 415, so no test changes server state.
"""
from __future__ import annotations

import socket
import threading
from collections.abc import Iterator
from typing import Any

import pytest

from coding_harness.modes import serve_mode

JSON = ("Content-Type", "application/json")
PASSED = 404
UI_ORIGIN = serve_mode.DEFAULT_UI_ORIGIN


def _start() -> Any:
    """Run serve_mode on a free loopback port and return the server."""
    ready: dict[str, Any] = {}
    started = threading.Event()

    def _ready(server: Any) -> None:
        ready["server"] = server
        started.set()

    threading.Thread(target=serve_mode.run, daemon=True, kwargs=dict(
        host="127.0.0.1", port=0, enable_mcp=False, ready_callback=_ready)).start()
    assert started.wait(timeout=5.0)
    return ready["server"]


def _serve(tmp_path_factory: pytest.TempPathFactory, auth: str | None) -> Iterator[int]:
    """Start a loopback server with HARNESS_AUTH set to ``auth``; yields its port."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HARNESS_AUTH_TOKEN_FILE", str(tmp_path_factory.mktemp("cfg") / "auth_token"))
        if auth is None:
            patch.delenv("HARNESS_AUTH", raising=False)
        else:
            patch.setenv("HARNESS_AUTH", auth)
        server = _start()
        yield server.server_port
        server.shutdown()
        server.server_close()


@pytest.fixture(scope="module")
def port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    """A loopback server with the login gate at its default."""
    yield from _serve(tmp_path_factory, None)


@pytest.fixture(scope="module")
def open_port(tmp_path_factory: pytest.TempPathFactory) -> Iterator[int]:
    """A loopback server started with HARNESS_AUTH=0."""
    yield from _serve(tmp_path_factory, "0")


def _send(port: int, method: str, path: str, host: str | None, *headers: tuple[str, str],
          body: bytes = b"", raw_body: bytes | None = None) -> int:
    """One request with exactly these headers; ``host`` None omits Host. Returns the status."""
    lines = [f"{method} {path} HTTP/1.1"]
    if host is not None:
        lines.append(f"Host: {host.replace('PORT', str(port))}")
    lines += [f"{k}: {v.replace('PORT', str(port))}" for k, v in headers]
    if raw_body is None and body:
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    payload = "\r\n".join(lines).encode() + b"\r\n\r\n" + (body if raw_body is None else raw_body)
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        sock.sendall(payload)
        data = sock.recv(4096)
    return int(data.split(b" ", 2)[1])


def _post(port: int, host: str | None, *headers: tuple[str, str]) -> int:
    """A JSON POST to the unknown route with these extra headers."""
    return _send(port, "POST", "/v1/nope", host, JSON, *headers, body=b"{}")


FOREIGN_HOSTS = [
    "evil.example",
    "evil.example:PORT",
    "EVIL.EXAMPLE:PORT",
    "evil.example.:PORT",
    "127.0.0.1@evil.example",
    "127.0.0.1:PORT@evil.example",
    "127.0.0.1.evil.example:PORT",
    "localhost.evil.example:PORT",
    "127.0.0.1.nip.io:PORT",
    "[::1].evil.example:PORT",
    "0.0.0.0:PORT",
    "[::]:PORT",
    "192.168.1.10:PORT",
]


@pytest.mark.parametrize("host", FOREIGN_HOSTS)
def test_foreign_host_is_refused_on_post_and_get(port: int, host: str) -> None:
    assert _post(port, host) == 403
    assert _send(port, "GET", "/v1/sessions", host) == 403
    assert _send(port, "GET", "/", host) == 403
    assert _send(port, "OPTIONS", "/v1/sessions", host) == 403


@pytest.mark.parametrize("host", [
    "127.0.0.1:PORT", "localhost:PORT", "[::1]:PORT", "LOCALHOST:PORT", "127.0.0.1", "LocalHost",
])
def test_loopback_host_is_accepted(port: int, host: str) -> None:
    assert _post(port, host) == PASSED
    assert _send(port, "GET", "/v1/sessions", host) == 200


@pytest.mark.parametrize("host", ["localhost.:PORT", "127.0.0.1.:PORT"])
def test_trailing_dot_loopback_host_fails_closed(port: int, host: str) -> None:
    assert _post(port, host) == 403


@pytest.mark.parametrize("host", ["", None])
def test_empty_or_missing_host_is_refused(port: int, host: str | None) -> None:
    """The gate's own rule: a direct request must name a loopback Host."""
    assert _send(port, "GET", "/v1/sessions", host) == 403
    assert _post(port, host) == 403


@pytest.mark.parametrize("host", ["", None])
def test_null_origin_with_no_host_is_refused(port: int, host: str | None) -> None:
    """``Origin: null`` has an empty netloc, which equals an empty Host."""
    assert _post(port, host, ("Origin", "null")) == 403


@pytest.mark.parametrize("origin", [
    "http://evil.example",
    "https://evil.example:PORT",
    "null",
    "",
    "http://127.0.0.1.evil.example:PORT",
    "http://evil.example@127.0.0.1:PORT",
    "http://127.0.0.1:1",
    "http://localhost:PORT",
    "http://localhost:5173.evil.example",
    "http://localhost:51730",
    "http://localhost:5173/",
    "http://evil.example/?http://localhost:5173",
])
def test_foreign_origin_is_refused(port: int, origin: str) -> None:
    assert _post(port, "127.0.0.1:PORT", ("Origin", origin)) == 403


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH", "FOO"])
def test_foreign_origin_is_refused_for_every_unsafe_method(port: int, method: str) -> None:
    status = _send(port, method, "/v1/nope", "127.0.0.1:PORT", JSON,
                   ("Origin", "http://evil.example"), body=b"{}")
    assert status == 403


def test_no_origin_json_post_passes(port: int) -> None:
    assert _post(port, "127.0.0.1:PORT") == PASSED
    assert _send(port, "POST", "/v1/nope", "127.0.0.1:PORT") == PASSED
    assert _send(port, "POST", "/v1/nope", "127.0.0.1:PORT",
                 ("Content-Type", "application/json; charset=utf-8"), body=b"{}") == PASSED


@pytest.mark.parametrize("host,origin", [
    ("127.0.0.1:PORT", "http://127.0.0.1:PORT"),
    ("localhost:PORT", "http://localhost:PORT"),
    ("[::1]:PORT", "http://[::1]:PORT"),
    ("127.0.0.1:PORT", UI_ORIGIN),
    ("localhost:PORT", UI_ORIGIN),
])
def test_own_and_ui_origin_pass(port: int, host: str, origin: str) -> None:
    assert _post(port, host, ("Origin", origin)) == PASSED
    assert _send(port, "PUT", "/v1/nope", host, JSON, ("Origin", origin), body=b"{}") == PASSED


def test_ui_origin_does_not_excuse_a_foreign_host(port: int) -> None:
    assert _post(port, "evil.example:PORT", ("Origin", UI_ORIGIN)) == 403
    assert _post(port, "evil.example:PORT", ("Origin", "http://evil.example:PORT")) == 403


def test_cross_origin_get_is_left_to_the_browser(port: int) -> None:
    status = _send(port, "GET", "/v1/sessions", "127.0.0.1:PORT", ("Origin", "http://evil.example"))
    assert status == 200


@pytest.mark.parametrize("ctype", [
    "text/plain",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=x",
    "text/plain; application/json",
    "application/json, text/plain",
    "application/jsonx",
    "",
])
@pytest.mark.parametrize("method", ["POST", "PUT"])
def test_non_json_body_is_refused(port: int, method: str, ctype: str) -> None:
    status = _send(port, method, "/v1/nope", "127.0.0.1:PORT", ("Content-Type", ctype), body=b"{}")
    assert status == 415


def test_body_without_content_type_is_refused(port: int) -> None:
    assert _send(port, "POST", "/v1/nope", "127.0.0.1:PORT", body=b"{}") == 415


@pytest.mark.parametrize("length", ["-1", "+2", "2, 2", "0x2", "abc"])
def test_malformed_content_length_counts_as_a_body(port: int, length: str) -> None:
    status = _send(port, "POST", "/v1/nope", "127.0.0.1:PORT", ("Content-Type", "text/plain"),
                   ("Content-Length", length), raw_body=b"{}")
    assert status == 415


def test_chunked_non_json_body_is_refused(port: int) -> None:
    """A body declared by Transfer-Encoding alone is still a body that must be JSON."""
    status = _send(port, "POST", "/v1/nope", "127.0.0.1:PORT", ("Content-Type", "text/plain"),
                   ("Transfer-Encoding", "chunked"), raw_body=b"2\r\n{}\r\n0\r\n\r\n")
    assert status == 415


def test_forwarded_request_is_left_to_the_login_gate(port: int) -> None:
    status = _post(port, "evil.example", ("X-Forwarded-For", "203.0.113.9"),
                   ("Origin", "http://evil.example"))
    assert status == 401
    assert _send(port, "POST", "/v1/nope", "bjorn.tailnet.example", ("X-Forwarded-For", "100.64.0.1"),
                 ("Content-Type", "text/plain"), body=b"{}") == 401


def test_forwarded_header_does_not_open_a_server_whose_login_gate_is_off(open_port: int) -> None:
    """With HARNESS_AUTH=0 nothing stands behind this gate, so a proxy header must not skip it.

    A rebinding page is same-origin with the server it rebound to and may add
    ``X-Forwarded-For`` to its own requests.
    """
    assert _post(open_port, "evil.example:PORT") == 403
    status = _post(open_port, "evil.example:PORT", ("X-Forwarded-For", "1.2.3.4"),
                   ("Origin", "http://evil.example:PORT"))
    assert status in (401, 403)
