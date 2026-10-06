"""HTTP transport for the Ollama client: split connect/read timeouts, bounded retries.

Exports: open_stream, read_timeout_s, with_retries, retry_scope, HTTPStatusError,
EmptyResponse. The read timeout is a per-recv idle timeout, not a total budget.
The 360 s default is the measured worst single step plus one neighbor's queue
wait (eval/results). ``retry_scope`` lets a caller with a wall-clock deadline
bound the retries of every with_retries call made inside it, so the model
clients need no extra parameters threaded through.
"""
from __future__ import annotations

import contextlib
import http.client
import os
import socket
import threading
import time
from collections.abc import Iterator
from typing import Any, Callable, TypeVar
from urllib.parse import urlsplit

DEFAULT_READ_TIMEOUT_S = 360.0
T = TypeVar("T")
_scope = threading.local()  # GLOBAL-STATE: per-thread retry limits set by retry_scope


class HTTPStatusError(RuntimeError):
    """Non-2xx response; ``code`` decides whether with_retries tries again."""

    def __init__(self, code: int, body: str) -> None:
        super().__init__(f"ollama HTTP {code}: {body[:500]}")
        self.code = code
        self.body = body


class EmptyResponse(RuntimeError):
    """A 200 whose body carried no choices; transient on Ollama, so retried."""


class _Response(http.client.HTTPResponse):
    """HTTPResponse that also closes its owning connection on close()."""

    conn: http.client.HTTPConnection | None = None

    def close(self) -> None:
        """Close the response body, then the socket it was read from."""
        super().close()
        if self.conn is not None:
            self.conn.close()


def read_timeout_s() -> float:
    """Seconds from HARNESS_READ_TIMEOUT, or the measured default of 360."""
    try:
        value = float(os.environ.get("HARNESS_READ_TIMEOUT", ""))
    except ValueError:
        return DEFAULT_READ_TIMEOUT_S
    return value if value > 0 else DEFAULT_READ_TIMEOUT_S


def open_stream(
    url: str,
    payload: bytes,
    *,
    connect_timeout: float = 10.0,
    read_timeout: float | None = None,
) -> http.client.HTTPResponse:
    """POST JSON bytes to ``url``; return the response for the caller to read and close.

    Raises HTTPStatusError on a 4xx/5xx after draining the body, socket.timeout
    when no byte arrives within ``read_timeout``, and OSError for connect faults.
    """
    if read_timeout is None:
        read_timeout = read_timeout_s()
    parts = urlsplit(url)
    conn_cls: Any = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parts.hostname, parts.port, timeout=connect_timeout)
    conn.response_class = _Response
    path = f"{parts.path or '/'}?{parts.query}" if parts.query else (parts.path or "/")
    try:
        conn.request("POST", path, body=payload,
                     headers={"Content-Type": "application/json", "Connection": "close"})
        conn.sock.settimeout(read_timeout)
        resp = conn.getresponse()
        resp.conn = conn
        if resp.status >= 400:
            raise HTTPStatusError(resp.status, resp.read().decode("utf-8", errors="replace"))
    except BaseException:
        conn.close()
        raise
    return resp


def _retryable(exc: BaseException) -> bool:
    """Connection faults, 5xx, and empty bodies retry; everything else surfaces."""
    if isinstance(exc, HTTPStatusError):
        return exc.code >= 500
    return isinstance(exc, (OSError, EmptyResponse))


def _surface(exc: BaseException) -> RuntimeError:
    """Wrap a transport fault in the RuntimeError shape session.py reports."""
    if isinstance(exc, RuntimeError):
        return exc
    wrapped = RuntimeError(f"ollama transport error: {exc}")
    wrapped.__cause__ = exc
    return wrapped


@contextlib.contextmanager
def retry_scope(
    *, deadline: float | None = None, attempts: int | None = None,
) -> Iterator[None]:
    """Bound every with_retries call on this thread inside the block.

    ``deadline`` is a time.monotonic() instant past which no retry starts;
    ``attempts`` caps the attempt count. Either None leaves that limit alone.
    """
    previous = getattr(_scope, "limits", None)
    _scope.limits = (deadline, attempts)
    try:
        yield
    finally:
        _scope.limits = previous


def _scoped(deadline: float | None, attempts: int) -> tuple[float | None, int]:
    """The (deadline, attempts) after the active retry_scope, if any, is applied."""
    scope_deadline, scope_attempts = getattr(_scope, "limits", None) or (None, None)
    if deadline is None:
        deadline = scope_deadline
    if scope_attempts is not None:
        attempts = min(attempts, scope_attempts)
    return deadline, max(1, attempts)


def with_retries(
    fn: Callable[[], T], *, attempts: int = 3, backoff: tuple[float, float] = (2, 8),
    deadline: float | None = None,
) -> T:
    """Call ``fn`` up to ``attempts`` times on transient faults; never on a read timeout.

    ``deadline`` (time.monotonic() seconds) stops the retries once a backoff
    wait would end past it: the caller's budget is gone, so surface the fault.
    """
    deadline, attempts = _scoped(deadline, attempts)
    last: BaseException | None = None
    for i in range(attempts):
        try:
            return fn()
        except socket.timeout as e:
            raise RuntimeError(f"ollama transport error: {e}") from e
        except (OSError, http.client.HTTPException, RuntimeError) as e:
            last = e
            if not _retryable(e):
                break
        if i == attempts - 1:
            break
        wait = min(backoff[0] * (2 ** i), backoff[1])
        if deadline is not None and time.monotonic() + wait >= deadline:
            break
        time.sleep(wait)
    assert last is not None
    raise _surface(last)
