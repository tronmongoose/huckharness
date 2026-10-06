"""MCP transport layer — stdio + HTTP/SSE.

Two implementations:

- :class:`StdioTransport` — subprocess + framed JSONL over stdin/stdout.
  Lifts the framing code that previously lived in ``client.py``. No
  semantic change for stdio servers; existing stdio tests must stay green.
- :class:`HTTPTransport` — POST for requests, SSE for server→client
  notifications, OAuth-aware. (Phase 2; not implemented in this commit.)

Both implementations call ``on_message`` for each parsed JSON-RPC
envelope they receive — the client demultiplexes by ``id`` into pending
Futures or treats the message as a notification. They call ``on_close``
once when the underlying pipe goes away so the client can fail any
still-pending Futures.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Protocol

from coding_harness.mcp.config import MCPServerConfig
from coding_harness.security import env_scrub

SHUTDOWN_GRACE_SECONDS = 2.0
STDERR_TAIL_MAX = 50
STDERR_TAIL_IN_SNAPSHOT = 5


class TransportError(RuntimeError):
    """Raised by transports on send/start/close failures.

    The client wraps these into :class:`MCPClientError` with the
    server name and a diagnostic tail.
    """


OnMessage = Callable[[dict[str, Any]], None]
OnClose = Callable[[str], None]

# HTTP transport tuning.
SSE_BACKOFF_INITIAL = 1.0
SSE_BACKOFF_MAX = 30.0
SSE_READ_TIMEOUT = 60.0     # idle close before reconnect
POST_TIMEOUT = 30.0
HTTP_TAIL_MAX = 20           # last-events ring buffer for diagnostics


class AuthProvider(Protocol):
    """Minimal auth interface the HTTPTransport needs.

    Concrete implementation lives in ``coding_harness.mcp.oauth`` — kept
    behind a Protocol so transport tests can substitute a stub without
    pulling in the OAuth state machine.
    """

    def authorization_header(self) -> str:
        """Return e.g. ``Bearer <access_token>``."""

    def refresh(self) -> None:
        """Refresh the cached token. Called on 401."""


class MCPTransport(Protocol):
    """One bidirectional pipe to an MCP server. Threading is internal."""

    def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        """Begin reading; install callbacks. Calling twice raises."""

    def send(self, message: dict[str, Any]) -> None:
        """Push one JSON-RPC envelope toward the server.

        Stdio writes a JSONL frame on stdin. HTTP POSTs to the base URL;
        if the server returns a synchronous reply in the response body,
        the transport must still call ``on_message`` so the client's
        pending-future path resolves normally.
        """

    def close(self) -> None:
        """Tear down. Idempotent."""

    def diagnostic_snapshot(self) -> str:
        """Best-effort string for error messages (stderr tail for stdio,
        last few SSE events for HTTP)."""


class StdioTransport:
    """Subprocess + framed JSONL transport.

    Mechanical lift of the framing logic previously in
    ``coding_harness.mcp.client.MCPClient``. Same SIGTERM/grace/SIGKILL
    teardown; same 50-line stderr ring buffer.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._closed = False
        self._on_message: OnMessage | None = None
        self._on_close: OnClose | None = None
        self._close_signalled = False
        self._stderr_tail: list[str] = []
        self._stderr_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        if self._proc is not None:
            raise TransportError(
                f"StdioTransport[{self.config.name}] already started"
            )
        if self.config.command is None:
            raise TransportError(
                f"StdioTransport[{self.config.name}] requires command"
            )

        self._on_message = on_message
        self._on_close = on_close

        # A server gets no credential-shaped variable it did not name in its
        # own ``env`` block.
        env = env_scrub.scrub(os.environ)
        env.update(self.config.env)

        try:
            self._proc = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=self.config.cwd,
                env=env,
                bufsize=0,
            )
        except (OSError, FileNotFoundError) as e:
            raise TransportError(
                f"failed to spawn MCP server '{self.config.name}': {e}"
            ) from e

        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"mcp-reader-{self.config.name}",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader = threading.Thread(
            target=self._stderr_loop,
            name=f"mcp-stderr-{self.config.name}",
            daemon=True,
        )
        self._stderr_reader.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is None:
            self._signal_close("transport never started")
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
        except Exception:
            pass
        self._signal_close("transport closed")

    # ── Wire ─────────────────────────────────────────────────────────

    def send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise TransportError(
                f"StdioTransport[{self.config.name}] not started"
            )
        if proc.poll() is not None:
            raise TransportError(
                f"StdioTransport[{self.config.name}] subprocess exited "
                f"(code={proc.returncode})"
            )
        line = json.dumps(message, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                proc.stdin.write(line.encode("utf-8"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                raise TransportError(
                    f"StdioTransport[{self.config.name}] write failed: {e}"
                ) from e

    def diagnostic_snapshot(self) -> str:
        with self._stderr_lock:
            tail = self._stderr_tail[-STDERR_TAIL_IN_SNAPSHOT:]
        return " | ".join(tail) or "(empty)"

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ── Reader threads ──────────────────────────────────────────────

    def _read_loop(self) -> None:  # UNBOUNDED-LOOP: reader thread
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        stdout = proc.stdout
        try:
            while not self._closed:
                line = stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                cb = self._on_message
                if cb is not None:
                    try:
                        cb(message)
                    except Exception:
                        # Callback errors must not kill the reader thread;
                        # the client is responsible for its own demux state.
                        pass
        finally:
            self._signal_close(
                f"subprocess closed stdout; stderr tail: "
                f"{self.diagnostic_snapshot()}"
            )

    def _stderr_loop(self) -> None:  # UNBOUNDED-LOOP: reader thread
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        stderr = proc.stderr
        try:
            while not self._closed:
                line = stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                with self._stderr_lock:
                    self._stderr_tail.append(text)
                    if len(self._stderr_tail) > STDERR_TAIL_MAX:
                        self._stderr_tail = self._stderr_tail[-STDERR_TAIL_MAX:]
        except Exception:
            pass

    def _signal_close(self, reason: str) -> None:
        if self._close_signalled:
            return
        self._close_signalled = True
        cb = self._on_close
        if cb is not None:
            try:
                cb(reason)
            except Exception:
                pass


class HTTPTransport:
    """POST + SSE transport with optional OAuth.

    Wire shape:
      - POST ``<base>/``           — one JSON-RPC envelope per call.
      - GET  ``<base>/sse``        — server→client notification stream.
      - ``Authorization`` header is sourced from the AuthProvider on
        every request (so refresh propagates without re-wiring).
      - 401 on POST → ``auth.refresh()`` + retry once. Second 401 raises.
      - 401 on SSE  → refresh + reconnect once with backoff.
      - Reconnect backoff is exponential, capped at 30s.

    Two response shapes are valid for POST per the MCP HTTP binding:
      - **Synchronous reply** (HTTP 200, JSON body) — the transport
        immediately delivers it to ``on_message`` so the client's
        pending-future path resolves.
      - **Async over SSE** (HTTP 202, empty body) — the response will
        arrive on the SSE stream.

    Stdlib only. ``urllib.request`` for both POST and the streaming GET.
    """

    def __init__(
        self,
        url: str,
        *,
        name: str = "http",
        auth: AuthProvider | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._base = url.rstrip("/")
        self.name = name
        self._auth = auth
        self._extra_headers = dict(extra_headers or {})
        self._on_message: OnMessage | None = None
        self._on_close: OnClose | None = None
        self._sse_thread: threading.Thread | None = None
        self._closed = False
        self._started = False
        self._close_signalled = False
        self._tail: list[str] = []
        self._tail_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self, on_message: OnMessage, on_close: OnClose) -> None:
        if self._started:
            raise TransportError(f"HTTPTransport[{self.name}] already started")
        self._started = True
        self._on_message = on_message
        self._on_close = on_close
        self._sse_thread = threading.Thread(
            target=self._sse_loop,
            name=f"mcp-sse-{self.name}",
            daemon=True,
        )
        self._sse_thread.start()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # SSE reader exits on next read iteration (it checks self._closed).
        self._signal_close("transport closed")

    # ── Wire: POST ──────────────────────────────────────────────────

    def send(self, message: dict[str, Any]) -> None:
        if not self._started:
            raise TransportError(f"HTTPTransport[{self.name}] not started")
        if self._closed:
            raise TransportError(f"HTTPTransport[{self.name}] closed")

        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        # 401 → refresh + retry once.
        for attempt in (0, 1):
            try:
                status, payload = self._do_post(body)
            except urllib.error.HTTPError as e:
                if e.code == 401 and self._auth is not None and attempt == 0:
                    self._note("POST 401 → refreshing token")
                    try:
                        self._auth.refresh()
                    except Exception as refresh_err:
                        raise TransportError(
                            f"HTTPTransport[{self.name}] auth refresh failed: {refresh_err}"
                        ) from refresh_err
                    continue
                raise TransportError(
                    f"HTTPTransport[{self.name}] POST failed: HTTP {e.code} {e.reason}"
                ) from e
            except urllib.error.URLError as e:
                raise TransportError(
                    f"HTTPTransport[{self.name}] POST failed: {e.reason}"
                ) from e
            # Success.
            if status == 200 and payload:
                # Synchronous reply: deliver via on_message so the client
                # pending-future path resolves.
                try:
                    parsed = json.loads(payload.decode("utf-8", errors="replace"))
                except json.JSONDecodeError as e:
                    raise TransportError(
                        f"HTTPTransport[{self.name}] POST body not JSON: {e}"
                    ) from e
                if isinstance(parsed, dict):
                    cb = self._on_message
                    if cb is not None:
                        try:
                            cb(parsed)
                        except Exception:
                            pass
            # 202 with empty body: response will arrive over SSE.
            return

    def _do_post(self, body: bytes) -> tuple[int, bytes]:
        req = urllib.request.Request(
            self._base + "/",
            data=body,
            method="POST",
            headers=self._headers(content_type="application/json"),
        )
        with urllib.request.urlopen(req, timeout=POST_TIMEOUT) as resp:
            return resp.status, resp.read()

    # ── Wire: SSE ───────────────────────────────────────────────────

    def _sse_loop(self) -> None:  # UNBOUNDED-LOOP: reader thread w/ kill switch
        backoff = SSE_BACKOFF_INITIAL
        refreshed_since_last_401 = False
        while not self._closed:
            try:
                self._consume_sse_once()
                # Stream ended cleanly (server closed). Reconnect with backoff.
                self._note("SSE stream closed; reconnecting")
            except _SSEUnauthorized:
                if self._auth is not None and not refreshed_since_last_401:
                    self._note("SSE 401 → refreshing token")
                    try:
                        self._auth.refresh()
                        refreshed_since_last_401 = True
                        backoff = SSE_BACKOFF_INITIAL
                        continue
                    except Exception as e:
                        self._note(f"SSE refresh failed: {e}")
                else:
                    self._note("SSE 401 after refresh; giving up on reconnect")
                    break
            except Exception as e:
                self._note(f"SSE error: {e}")
                refreshed_since_last_401 = False
            # Backoff before reconnect.
            sleep_for = min(backoff, SSE_BACKOFF_MAX)
            stop_at = time.monotonic() + sleep_for
            while not self._closed and time.monotonic() < stop_at:
                time.sleep(0.1)
            backoff = min(backoff * 2, SSE_BACKOFF_MAX)
        self._signal_close("sse loop exited")

    def _consume_sse_once(self) -> None:
        req = urllib.request.Request(
            self._base + "/sse",
            method="GET",
            headers=self._headers(accept="text/event-stream"),
        )
        try:
            response = urllib.request.urlopen(req, timeout=SSE_READ_TIMEOUT)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise _SSEUnauthorized() from e
            raise
        try:
            event_data: list[str] = []
            while not self._closed:
                raw = response.readline()
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if event_data:
                        self._dispatch_event("\n".join(event_data))
                        event_data = []
                    continue
                if line.startswith(":"):
                    continue  # SSE comment / keepalive
                if line.startswith("data:"):
                    event_data.append(line[5:].lstrip())
                # Ignore event: / id: / retry: — we don't need them.
        finally:
            try:
                response.close()
            except Exception:
                pass

    def _dispatch_event(self, payload: str) -> None:
        if not payload:
            return
        with self._tail_lock:
            self._tail.append(payload[:200])
            if len(self._tail) > HTTP_TAIL_MAX:
                self._tail = self._tail[-HTTP_TAIL_MAX:]
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            return
        if not isinstance(parsed, dict):
            return
        cb = self._on_message
        if cb is not None:
            try:
                cb(parsed)
            except Exception:
                pass

    # ── Diagnostics ────────────────────────────────────────────────

    def diagnostic_snapshot(self) -> str:
        with self._tail_lock:
            tail = self._tail[-STDERR_TAIL_IN_SNAPSHOT:]
        return " | ".join(tail) or "(empty)"

    # ── Internal ────────────────────────────────────────────────────

    def _headers(
        self,
        *,
        content_type: str | None = None,
        accept: str | None = None,
    ) -> dict[str, str]:
        h: dict[str, str] = {"User-Agent": "coding_harness/mcp"}
        if content_type:
            h["Content-Type"] = content_type
        if accept:
            h["Accept"] = accept
        if self._auth is not None:
            try:
                h["Authorization"] = self._auth.authorization_header()
            except Exception as e:
                raise TransportError(
                    f"HTTPTransport[{self.name}] auth header failed: {e}"
                ) from e
        h.update(self._extra_headers)
        return h

    def _note(self, text: str) -> None:
        with self._tail_lock:
            self._tail.append(text)
            if len(self._tail) > HTTP_TAIL_MAX:
                self._tail = self._tail[-HTTP_TAIL_MAX:]

    def _signal_close(self, reason: str) -> None:
        if self._close_signalled:
            return
        self._close_signalled = True
        cb = self._on_close
        if cb is not None:
            try:
                cb(reason)
            except Exception:
                pass


class _SSEUnauthorized(Exception):
    """Internal: SSE GET returned 401. Caught by ``_sse_loop`` to drive refresh."""


def build_transport(
    config: MCPServerConfig, *, headless: bool = False
) -> MCPTransport:
    """Construct the right transport for a server config.

    Dispatch on whether the config declares ``url`` (HTTP) or
    ``command`` (stdio). When OAuth is enabled, instantiates an
    :class:`OAuthSession` lazily and binds it to the HTTPTransport so
    refresh-on-401 propagates without re-wiring.

    ``headless`` short-circuits the OAuth first-run browser flow with
    :exc:`OAuthRequiresConsent` — pipelines running under a scheduler
    pass ``headless=True`` so they fail-fast instead of hanging on a
    consent dialog that no one will click.
    """
    if config.url is not None:
        auth: AuthProvider | None = None
        if config.oauth:
            # Local import: stdio-only configs shouldn't pull in OAuth state.
            from coding_harness.mcp.oauth import OAuthSession

            auth = OAuthSession(
                name=config.name,
                base_url=config.url,
                scopes=config.oauth_scopes,
                headless=headless,
            )
        return HTTPTransport(config.url, name=config.name, auth=auth)
    return StdioTransport(config)


def _ignore_sigpipe() -> None:
    """Best-effort: don't kill the parent on a transient broken-pipe write
    against a server that died mid-flight. Called on import on POSIX.

    SIG_IGN, so the write raises BrokenPipeError for the caller to handle.
    This once set SIG_DFL, which *kills* the process on any broken pipe: a
    browser dropping an SSE stream took the whole serve process down, and
    the suite had to run file by file to dodge it.
    """
    if hasattr(signal, "SIGPIPE"):
        try:
            signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        except (OSError, ValueError):
            pass


_ignore_sigpipe()
