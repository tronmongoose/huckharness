"""Synchronous MCP client — transport-agnostic JSON-RPC.

MCP is JSON-RPC 2.0. This client speaks it over whichever transport its
config selects: stdio today, HTTP/SSE next. The dispatch shape is
synchronous from the caller's perspective — the existing ``Tool.run()``
contract is sync, and adding asyncio to the hot path would be gratuitous
complexity. A reader thread (owned by the transport) demultiplexes
responses by ``id`` into ``concurrent.futures.Future`` objects via the
``_on_message`` callback; the call site blocks on
``future.result(timeout=...)``.

Lifecycle:

    client = MCPClient(MCPServerConfig(...))
    client.start()                       # spawn + initialize + tools/list
    tools = client.list_tools()          # cached after start()
    result = client.call_tool(name, args, timeout=30.0)
    client.close()                       # idempotent; transport-managed teardown

Errors raise ``MCPClientError`` (with the JSON-RPC error attached when
present). The caller is responsible for translating those into the
harness ``ToolResult`` shape — see ``mcp.tool_adapter.MCPTool``.
"""
from __future__ import annotations

import threading
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from typing import Any

from coding_harness.mcp.config import MCPServerConfig
from coding_harness.mcp.transport import (
    MCPTransport,
    TransportError,
    build_transport,
)

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "coding_harness"
CLIENT_VERSION = "0.1.0"

DEFAULT_REQUEST_TIMEOUT = 30.0
INIT_TIMEOUT = 15.0


class MCPClientError(RuntimeError):
    """Raised for transport, protocol, or remote-side errors.

    ``rpc_error`` carries the JSON-RPC error object when the failure came from
    the server itself (so callers can surface ``code`` and ``message``).
    """

    def __init__(self, message: str, *, rpc_error: dict[str, Any] | None = None):
        super().__init__(message)
        self.rpc_error = rpc_error


@dataclass
class _Pending:
    future: Future[dict[str, Any]] = field(default_factory=Future)


class MCPClient:
    """One MCP server, one transport, one bidirectional pipe.

    Thread-safety: ``call_tool`` and ``list_tools`` are safe to call from
    multiple threads. ``start`` and ``close`` should be called from the owning
    thread (typically the Session's main thread).
    """

    def __init__(
        self,
        config: MCPServerConfig,
        *,
        transport: MCPTransport | None = None,
    ):
        self.config = config
        self._transport: MCPTransport = (
            transport if transport is not None else build_transport(config)
        )
        self._pending: dict[int, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._started = False
        self._closed = False
        self._tools_cache: list[dict[str, Any]] | None = None

    # ── Lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the transport, perform ``initialize`` + ``initialized``."""
        if self._started:
            raise MCPClientError(
                f"MCPClient[{self.config.name}] already started"
            )
        self._started = True
        try:
            self._transport.start(self._on_message, self._on_close)
        except TransportError as e:
            raise MCPClientError(
                f"MCPClient[{self.config.name}] transport start failed: {e}"
            ) from e

        try:
            self._initialize()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Tear down. Idempotent."""
        if self._closed:
            return
        self._closed = True
        try:
            self._transport.close()
        except Exception:
            pass
        self._drain_pending("MCPClient closed before response")

    def is_alive(self) -> bool:
        """Best-effort liveness for stdio transports. Other transports
        return True until ``close`` is called."""
        check = getattr(self._transport, "is_alive", None)
        if callable(check):
            return bool(check())
        return self._started and not self._closed

    # ── Public RPC surface ───────────────────────────────────────────

    def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Return the cached tools list. Set ``refresh=True`` to re-query."""
        if self._tools_cache is None or refresh:
            response = self._request("tools/list", {}, timeout=INIT_TIMEOUT)
            tools = response.get("tools") or []
            if not isinstance(tools, list):
                raise MCPClientError(
                    f"tools/list: expected list, got {type(tools).__name__}"
                )
            self._tools_cache = tools
        return list(self._tools_cache)

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Invoke ``tools/call`` and return the raw MCP result envelope.

        The result envelope has the MCP shape ``{"content": [...],
        "isError": bool}``. Translation to ``ToolResult`` happens in
        ``MCPTool.run``.
        """
        return self._request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=timeout,
        )

    # ── Internal: initialize handshake ───────────────────────────────

    def _initialize(self) -> None:
        params = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {
                "name": CLIENT_NAME,
                "version": CLIENT_VERSION,
            },
        }
        self._request("initialize", params, timeout=INIT_TIMEOUT)
        # ``initialized`` is a notification (no id, no response).
        self._notify("notifications/initialized", {})

    # ── Internal: request/notify ─────────────────────────────────────

    def _next_request_id(self) -> int:
        with self._id_lock:
            request_id = self._next_id
            self._next_id += 1
            return request_id

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        request_id = self._next_request_id()
        pending = _Pending()
        with self._pending_lock:
            self._pending[request_id] = pending

        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }
        try:
            self._send(message)
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise

        try:
            response = pending.future.result(timeout=timeout)
        except FuturesTimeoutError as e:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise MCPClientError(
                f"MCPClient[{self.config.name}] timeout on {method} "
                f"after {timeout:.1f}s"
            ) from e

        if "error" in response:
            err = response["error"] or {}
            raise MCPClientError(
                f"MCPClient[{self.config.name}] {method} error: "
                f"{err.get('message', 'unknown')} (code={err.get('code')})",
                rpc_error=err if isinstance(err, dict) else None,
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise MCPClientError(
                f"MCPClient[{self.config.name}] {method} result missing or not an object"
            )
        return result

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        message = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        self._send(message)

    def _send(self, message: dict[str, Any]) -> None:
        try:
            self._transport.send(message)
        except TransportError as e:
            raise MCPClientError(
                f"MCPClient[{self.config.name}] send failed: {e}; "
                f"stderr tail: {self._transport.diagnostic_snapshot()}"
            ) from e

    # ── Internal: transport callbacks ────────────────────────────────

    def _on_message(self, message: dict[str, Any]) -> None:
        msg_id = message.get("id")
        if msg_id is None:
            # Notification or log line — no caller to deliver to.
            return
        with self._pending_lock:
            pending = self._pending.pop(msg_id, None)
        if pending is None:
            return
        if not pending.future.done():
            pending.future.set_result(message)

    def _on_close(self, reason: str) -> None:
        self._drain_pending(reason)

    def _drain_pending(self, reason: str) -> None:
        with self._pending_lock:
            pending_items = list(self._pending.items())
            self._pending.clear()
        for _id, p in pending_items:
            if not p.future.done():
                p.future.set_exception(
                    MCPClientError(
                        f"MCPClient[{self.config.name}]: {reason}"
                    )
                )
