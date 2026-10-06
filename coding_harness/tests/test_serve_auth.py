"""The login gate `bjorn serve` puts in front of /v1 on a non-loopback bind.

The proxied-loopback tests bind 127.0.0.1 for real and add the headers a
reverse proxy such as `tailscale serve` would. A real non-loopback bind would expose the test server to the network, so the
server binds 127.0.0.1 and these tests monkeypatch ``serve_auth.is_loopback``
to answer False. The gate reads only that function to decide, so the request
path is the same one a 0.0.0.0 or tailnet bind takes.
"""
from __future__ import annotations

import json
import socket
import stat
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from coding_harness import cli
from coding_harness.modes import serve_auth, serve_mode


def _start(host: str = "127.0.0.1") -> Any:
    """Run serve_mode on a free loopback port and return the server."""
    ready: dict[str, Any] = {}
    started = threading.Event()

    def _ready(server: Any) -> None:
        ready["server"] = server
        started.set()

    threading.Thread(target=serve_mode.run, daemon=True, kwargs=dict(
        host=host, port=0, enable_mcp=False, ready_callback=_ready)).start()
    assert started.wait(timeout=5.0)
    return ready["server"]


def _call(base: str, path: str, method: str = "GET", body: Any = None,
          cookie: str | None = None,
          headers: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
    """One request; returns status, parsed JSON (or raw bytes) and headers."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if cookie:
        req.add_header("Cookie", cookie)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    try:
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            status, raw, headers = resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as e:
        status, raw, headers = e.code, e.read(), dict(e.headers)
    try:
        return status, json.loads(raw), headers
    except ValueError:
        return status, raw, headers


@pytest.fixture
def token_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the token file at a temp path and clear HARNESS_AUTH."""
    path = tmp_path / "cfg" / "auth_token"
    monkeypatch.setenv("HARNESS_AUTH_TOKEN_FILE", str(path))
    monkeypatch.delenv("HARNESS_AUTH", raising=False)
    return path


@pytest.fixture
def remote(token_file: Path, monkeypatch: pytest.MonkeyPatch):
    """A server whose gate believes it is bound to a non-loopback address."""
    monkeypatch.setattr(serve_auth, "is_loopback", lambda host: False)
    server = _start()
    yield f"http://127.0.0.1:{server.server_port}", token_file
    server.shutdown()
    server.server_close()


def _login(base: str, token: str,
           headers: dict[str, str] | None = None) -> tuple[int, Any, dict[str, str]]:
    """POST /v1/auth with a token."""
    return _call(base, "/v1/auth", "POST", {"token": token}, headers=headers)


def _session_cookie(headers: dict[str, str]) -> str:
    """The name=value pair from a Set-Cookie header."""
    return headers["Set-Cookie"].split(";", 1)[0]


def test_loopback_bind_enforces_nothing(token_file: Path) -> None:
    server = _start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        assert _call(base, "/v1/healthz")[0] == 200
        assert _call(base, "/v1/auth/status")[1] == {"required": False, "authenticated": True}
        assert not token_file.exists()
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("method,path", [
    ("GET", "/v1/healthz"),
    ("GET", "/v1/sessions"),
    ("GET", "/v1/sessions/nope/events"),
    ("GET", "/v1/zzz"),
    ("POST", "/v1/zzz"),
    ("POST", "/v1/sessions"),
    ("POST", "/v1/projects/open"),
    ("PUT", "/v1/sessions/nope/plan"),
    ("POST", "/v1/auth/logout"),
])
def test_every_api_route_needs_a_session(remote, method: str, path: str) -> None:
    base, _ = remote
    status, body, _ = _call(base, path, method, {} if method != "GET" else None)
    assert (status, body) == (401, {"error": "unauthorized"})


def test_status_and_preflight_stay_open(remote) -> None:
    base, _ = remote
    status, body, _ = _call(base, "/v1/auth/status")
    assert (status, body) == (200, {"required": True, "authenticated": False,
                                    "token_path_hint": "cfg/auth_token"})
    assert _call(base, "/v1/sessions", "OPTIONS")[0] == 204


def test_ui_shell_is_not_gated(remote) -> None:
    base, _ = remote
    assert _call(base, "/")[0] != 401


def test_login_sets_cookie_and_opens_routes(remote) -> None:
    base, token_file = remote
    status, body, headers = _login(base, token_file.read_text().strip())
    assert (status, body) == (200, {"ok": True, "required": True})
    attrs = [a.strip() for a in headers["Set-Cookie"].split(";")]
    assert attrs[0].startswith("bjorn_session=")
    assert {"HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=2592000"} <= set(attrs)
    assert "Secure" not in attrs
    cookie = _session_cookie(headers)
    assert _call(base, "/v1/healthz", cookie=cookie)[0] == 200
    assert _call(base, "/v1/auth/status", cookie=cookie)[1]["authenticated"] is True
    # Past the gate the route table answers, so a made-up route is a 404 now.
    assert _call(base, "/v1/zzz", cookie=cookie)[0] == 404
    assert _call(base, "/v1/sessions/nope/events", cookie=cookie)[0] == 404


def test_forwarded_https_marks_the_cookie_secure(remote) -> None:
    base, token_file = remote
    req = urllib.request.Request(
        base + "/v1/auth", method="POST",
        data=json.dumps({"token": token_file.read_text().strip()}).encode(),
        headers={"Content-Type": "application/json", "X-Forwarded-Proto": "https"})
    with urllib.request.urlopen(req, timeout=5.0) as resp:
        assert "Secure" in [a.strip() for a in resp.headers["Set-Cookie"].split(";")]


def test_wrong_token_and_forged_cookie_are_refused(remote) -> None:
    base, _ = remote
    status, body, headers = _login(base, "not-the-token")
    assert status == 401 and body["error"] == "unauthorized"
    assert "Set-Cookie" not in headers
    assert _call(base, "/v1/healthz", cookie="bjorn_session=forged")[0] == 401


def test_sixth_attempt_is_locked_out_even_with_the_right_token(remote) -> None:
    base, token_file = remote
    lefts = [_login(base, f"wrong-{i}")[1]["attempts_left"] for i in range(5)]
    assert lefts == [4, 3, 2, 1, 0]
    status, body, headers = _login(base, token_file.read_text().strip())
    assert status == 429 and body["error"] == "locked"
    assert 0 < int(headers["Retry-After"]) <= 61


def test_logout_invalidates_the_session(remote) -> None:
    base, token_file = remote
    cookie = _session_cookie(_login(base, token_file.read_text().strip())[2])
    status, _, headers = _call(base, "/v1/auth/logout", "POST", {}, cookie=cookie)
    assert status == 200 and "Max-Age=0" in headers["Set-Cookie"]
    assert _call(base, "/v1/healthz", cookie=cookie)[0] == 401


def test_harness_auth_zero_disables_the_gate(token_file: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(serve_auth, "is_loopback", lambda host: False)
    monkeypatch.setenv("HARNESS_AUTH", "0")
    server = _start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        assert _call(base, "/v1/healthz")[0] == 200
        assert _call(base, "/v1/auth/status")[1]["required"] is False
    finally:
        server.shutdown()
        server.server_close()
    assert "WARNING" in serve_auth.startup_lines("0.0.0.0")[0]


def test_serve_creates_token_0600_and_never_prints_it(
        token_file: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(serve_mode, "run", lambda **kw: 0)
    assert cli._run_serve(["--host", "0.0.0.0", "--no-mcp"]) == 0
    token = token_file.read_text().strip()
    assert len(token) >= 43
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    out = capsys.readouterr()
    assert token not in out.out + out.err
    assert f"GUI login token is in {token_file}" in out.err
    assert cli._run_serve(["--host", "0.0.0.0", "--no-mcp"]) == 0
    assert token_file.read_text().strip() == token


def test_loopback_names() -> None:
    for host in ("127.0.0.1", "::1", "localhost", "127.0.0.2", "[::1]"):
        assert serve_auth.is_loopback(host), host
    for host in ("0.0.0.0", "100.64.1.2", "::", "example.ts.net"):
        assert not serve_auth.is_loopback(host), host


@pytest.fixture
def local(token_file: Path):
    """A real loopback bind, the target `tailscale serve 9100` proxies to."""
    server = _start()
    yield f"http://127.0.0.1:{server.server_port}", token_file
    server.shutdown()
    server.server_close()


_TS = {"X-Forwarded-For": "100.64.0.7", "Tailscale-User-Login": "erik@example.com"}


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP",
                                    "Tailscale-User-Login"])
def test_proxied_request_on_loopback_needs_a_session(local, header: str) -> None:
    base, token_file = local
    proxy = {header: "for=100.64.0.7" if header == "Forwarded" else "100.64.0.7"}
    assert _call(base, "/v1/sessions", headers=proxy)[0] == 401
    assert _call(base, "/v1/auth/status", headers=proxy)[1] == {
        "required": True, "authenticated": False, "token_path_hint": "cfg/auth_token"}
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_proxied_login_works_and_status_follows(local) -> None:
    base, token_file = local
    assert _call(base, "/v1/auth/status", headers=_TS)[1]["authenticated"] is False
    status, _, headers = _login(base, token_file.read_text().strip(), headers=_TS)
    assert status == 200
    cookie = _session_cookie(headers)
    assert _call(base, "/v1/sessions", cookie=cookie, headers=_TS)[0] == 200
    assert _call(base, "/v1/auth/status", cookie=cookie, headers=_TS)[1] == {
        "required": True, "authenticated": True, "token_path_hint": "cfg/auth_token"}


def test_plain_loopback_request_stays_open_beside_proxied_ones(local) -> None:
    base, token_file = local
    assert _call(base, "/v1/sessions")[0] == 200
    assert not token_file.exists()
    assert _call(base, "/v1/sessions", headers=_TS)[0] == 401
    assert _call(base, "/v1/sessions")[0] == 200


def test_lockout_keys_on_the_tailscale_login_from_this_host(local) -> None:
    base, token_file = local
    alice = {**_TS, "Tailscale-User-Login": "alice@example.com"}
    for i in range(5):
        _login(base, f"wrong-{i}", headers=alice)
    assert _login(base, "wrong", headers=alice)[0] == 429
    # Another tailnet user behind the same proxy address is not locked out.
    assert _login(base, token_file.read_text().strip(), headers=_TS)[0] == 200


def _raw_login(port: int, head: str, body: bytes = b"") -> str:
    """Send a hand-written POST /v1/auth and return the status line."""
    with socket.create_connection(("127.0.0.1", port), timeout=5.0) as sock:
        sock.sendall(f"POST /v1/auth HTTP/1.1\r\nHost: x\r\n{head}\r\n".encode() + body)
        return sock.recv(4096).decode("latin-1").split("\r\n", 1)[0]


@pytest.mark.parametrize("head", [
    "Content-Type: application/json\r\nContent-Length: 999999999\r\n",
    "Content-Type: application/json\r\n",
    "Content-Type: application/json\r\nContent-Length: lots\r\n",
    "Content-Type: application/json\r\nContent-Length: 4097\r\n",
])
def test_oversized_or_unsized_login_body_is_refused_unread(remote, head: str) -> None:
    base, _ = remote
    port = int(base.rsplit(":", 1)[1])
    # The server answers without waiting for a body it was promised.
    assert " 413 " in _raw_login(port, head)


def test_login_requires_a_json_content_type(remote) -> None:
    base, token_file = remote
    port = int(base.rsplit(":", 1)[1])
    body = json.dumps({"token": token_file.read_text().strip()}).encode()
    head = f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
    assert " 415 " in _raw_login(port, head, body)


def test_token_path_hint_never_carries_the_full_path(tmp_path: Path) -> None:
    hint = serve_auth.token_path_hint(tmp_path / "deep" / "secrets" / "tok")
    assert hint == "secrets/tok"
    assert str(tmp_path) not in hint
