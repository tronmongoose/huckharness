"""Board fan-in hardening: no proxy, capped bodies, one shared deadline, bad HTTP.

Each origin is a throwaway loopback socket server scripted per test, so the
fetch path runs for real without a harness server behind it.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from coding_harness.modes import serve_board

TRICKLE = b"<trickle>"
_BOARD = {"server": {"cwd": "/p/x", "port": 1, "pid": 1}, "sessions": []}


def _http(body: bytes) -> bytes:
    """A complete HTTP/1.0 200 response carrying ``body``."""
    head = f"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    return head.encode() + body


class _Origin:
    """A loopback listener answering each connection with ``reply`` (None: never answer)."""

    def __init__(self, reply: bytes | None):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.reply = reply
        self.accepted = 0
        self.url = f"http://127.0.0.1:{self.sock.getsockname()[1]}"
        if reply is not None:
            threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        """Answer up to 8 connections, then stop."""
        for _ in range(8):
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            with conn:
                conn.recv(65536)
                if self.reply == TRICKLE:
                    self._trickle(conn)
                else:
                    conn.sendall(self.reply or b"")

    def _trickle(self, conn: socket.socket) -> None:
        """One header byte every 0.3 s for 3 s: never idle long enough for a socket timeout."""
        for _ in range(10):
            try:
                conn.sendall(b"H")
            except OSError:
                return
            time.sleep(0.3)

    def close(self) -> None:
        self.sock.close()


@pytest.fixture()
def origins():
    """Factory for scripted origins, all closed at teardown."""
    made: list[_Origin] = []

    def _make(reply: bytes | None) -> _Origin:
        o = _Origin(reply)
        made.append(o)
        return o

    yield _make
    for o in made:
        o.close()


def test_fan_in_ignores_http_proxy(origins, monkeypatch):
    good = origins(_http(json.dumps(_BOARD).encode()))
    proxy = origins(_http(json.dumps(_BOARD).encode()))
    for var in ("http_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(var, proxy.url)
    for var in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    out = serve_board.fan_in([good.url])
    assert [s["origin"] for s in out["servers"]] == [good.url]
    assert proxy.accepted == 0 and good.accepted == 1


def test_fan_in_caps_the_body(origins, monkeypatch):
    monkeypatch.setattr(serve_board, "MAX_BOARD_BYTES", 1000)
    big = origins(_http(json.dumps({**_BOARD, "pad": "x" * 5000}).encode()))
    out = serve_board.fan_in([big.url])
    assert out["servers"] == []
    assert out["errors"][0]["origin"] == big.url
    assert "over 1000 bytes" in out["errors"][0]["error"]


def test_fan_in_shares_one_deadline(origins):
    slow = [origins(TRICKLE), origins(TRICKLE)]
    start = time.monotonic()
    out = serve_board.fan_in([o.url for o in slow])
    assert time.monotonic() - start < 1.5
    assert out["servers"] == []
    assert sorted(e["origin"] for e in out["errors"]) == sorted(o.url for o in slow)
    assert {e["error"] for e in out["errors"]} == {"timeout"}


def test_fan_in_reports_a_bad_status_line(origins):
    bad = origins(b"GARBAGE\r\n")
    out = serve_board.fan_in([bad.url])
    assert out["servers"] == []
    assert [e["origin"] for e in out["errors"]] == [bad.url]
    assert out["errors"][0]["error"] != "timeout"


def test_loopback_origin_must_match_whole():
    assert serve_board._LOOPBACK.fullmatch("http://127.0.0.1:8080")
    assert not serve_board._LOOPBACK.fullmatch("http://127.0.0.1:8080.evil.test")
