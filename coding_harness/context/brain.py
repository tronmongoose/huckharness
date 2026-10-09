"""The second brain: a searchable note index behind a swappable backend.

Exports ``TIER``, ``tier_of``, ``allowed_tier``, ``Hit``, ``Backend``,
``MCPBackend``, ``BACKENDS``, ``backend_for``, ``BrainClient``, ``BrainError``,
``client_for``, ``STALE_HOURS``, ``RECALL_OFF_HOURS`` and ``is_stale``. The index (slos-recall on this machine) is configured by
the ``brain`` block in the user's settings. ``MCPBackend`` reads it as an
``.mcp.json`` server entry plus ``agent_id`` and spawns that server as a
child process. The in-process backend (``brain_inprocess``) calls the
slos_recall library directly; ``backend_for`` says which one a block gets.

Privacy rule: every hit carries a sensitivity tier. What reaches a *model* is
capped by ``allowed_tier``: everything on local Ollama, internal and below on
any frontier backend. A tier this module does not recognise counts as the
highest, so an unlabelled note is withheld rather than leaked.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from coding_harness.mcp.client import MCPClient, MCPClientError
from coding_harness.mcp.config import MCPServerConfig, _expand_obj

TIER = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
LOCAL_MAX, FRONTIER_MAX = 3, 1
CALL_TIMEOUT_S = 30.0
STALE_HOURS = 30.0       # the index's own daily window: older than this is flagged
RECALL_OFF_HOURS = 168.0  # a week: automatic recall stops feeding notes this old


def is_stale(age_hours: float | None) -> bool:
    """True past the daily window; an unknown age is never stale."""
    return age_hours is not None and age_hours > STALE_HOURS


class BrainError(RuntimeError):
    """The index is unconfigured, unreachable, or refused the request."""


def tier_of(sensitivity: object) -> int:
    """Numeric tier; an unknown label is treated as restricted (fail closed)."""
    return TIER.get(str(sensitivity or "").lower(), TIER["restricted"])


def allowed_tier(local: bool) -> int:
    """The highest tier a model may receive: all of it locally, internal on a frontier backend."""
    return LOCAL_MAX if local else FRONTIER_MAX


@dataclass
class Hit:
    """One search result."""

    path: str
    sensitivity: str
    score: float
    snippet: str

    @property
    def vault(self) -> str:
        return self.path.split("/", 1)[0] if "/" in self.path else ""

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "sensitivity": self.sensitivity, "score": self.score,
                "snippet": self.snippet, "vault": self.vault, "tier": tier_of(self.sensitivity)}


def _payload(result: dict[str, Any]) -> dict[str, Any]:
    """The JSON object inside an MCP text content block, raising on a tool error."""
    texts = [c.get("text", "") for c in result.get("content") or [] if c.get("type") == "text"]
    try:
        data = json.loads("".join(texts) or "{}")
    except ValueError as e:
        raise BrainError(f"unreadable reply from the index: {e}") from e
    if result.get("isError") or (isinstance(data, dict) and data.get("error")):
        raise BrainError(str(data.get("error") if isinstance(data, dict) else data))
    return data if isinstance(data, dict) else {}


class Backend(Protocol):
    """What a brain backend answers. Tier caps stay in the caller, never here.

    Optional, screen only: ``clusters(max_tier)`` and ``query_clusters(q,
    max_tier, warnings)`` for the topic map (``brain_clusters``). A backend
    without them has no map, and ``BrainClient`` answers None.
    """

    def search(self, query: str, *, top_k: int,
               warnings: list[str] | None = None) -> list[Hit]:
        """Up to ``top_k`` hits for ``query`` across every vault; degradations go to ``warnings``."""

    def page(self, path: str) -> dict[str, Any]:
        """One note: {path, sensitivity, content}."""

    def age_hours(self) -> float | None:
        """Hours since the last full index run; None when unknown."""

    def probe(self) -> float | None:
        """Raise BrainError unless the index answers for this identity; return its age."""

    def close(self) -> None:
        """Release whatever the backend holds."""


class MCPBackend:
    """One long-lived MCP connection, started on first use and restarted if it dies."""

    def __init__(self, block: dict[str, Any]):
        expanded = _expand_obj(block, dict(os.environ))
        assert isinstance(expanded, dict)
        command = expanded.get("command")
        if not isinstance(command, str) or not command:
            raise BrainError("brain.command must be a non-empty string")
        self.agent_id = str(expanded.get("agent_id") or "")
        env = {str(k): str(v) for k, v in (expanded.get("env") or {}).items()}
        if self.agent_id:
            env.setdefault("SLOS_RECALL_AGENT_ID", self.agent_id)
        self._config = MCPServerConfig(
            name="brain", command=command, args=[str(a) for a in expanded.get("args") or []],
            cwd=expanded.get("cwd"), env=env,
        )
        self._client: MCPClient | None = None
        self._lock = threading.Lock()

    def _call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """One tool call, restarting the server once after a dead pipe."""
        with self._lock:
            for attempt in range(2):
                if self._client is None or not self._client.is_alive():
                    self._client = MCPClient(self._config)
                    try:
                        self._client.start()
                    except MCPClientError as e:
                        self._client = None
                        raise BrainError(f"could not start the index server: {e}") from e
                try:
                    return _payload(self._client.call_tool(
                        tool, {**args, "agent_id": self.agent_id}, timeout=CALL_TIMEOUT_S))
                except MCPClientError as e:
                    self._client = None
                    if attempt == 1:
                        raise BrainError(f"index call failed: {e}") from e
        raise BrainError("unreachable")

    def search(self, query: str, *, top_k: int,
               warnings: list[str] | None = None) -> list[Hit]:
        """The server's hits, unfiltered. MCP reports no degradations."""
        data = self._call("search", {"query": query, "top_k": top_k})
        return [Hit(str(h.get("page_path")), str(h.get("sensitivity") or ""),
                    float(h.get("score") or 0.0), str(h.get("chunk_text") or ""))
                for h in data.get("hits") or [] if isinstance(h, dict)]

    def page(self, path: str) -> dict[str, Any]:
        """One note from the server."""
        data = self._call("get_page", {"path": path})
        return {"path": str(data.get("path") or path),
                "sensitivity": str(data.get("sensitivity") or ""),
                "content": str(data.get("content") or "")}

    def age_hours(self) -> float | None:
        """The MCP tools do not report index age."""
        return None

    def probe(self) -> float | None:
        """One cheap search: the only proof the child answers. Age stays unknown."""
        self.search("index", top_k=1)
        return None

    def close(self) -> None:
        """Stop the child server."""
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None


BACKENDS = ("mcp", "inprocess")


def backend_for(block: dict[str, Any]) -> Backend:
    """The backend a settings block asks for.

    An explicit ``backend`` wins and never falls back: ``inprocess`` without
    the library is an error, not a quiet MCP spawn. With no ``backend`` key,
    a block without ``command`` goes in-process when slos_recall is
    importable; everything else is MCP, which still requires ``command``.
    """
    from coding_harness.context import brain_inprocess

    choice = block.get("backend")
    if choice == "mcp":
        return MCPBackend(block)
    if choice == "inprocess":
        return brain_inprocess.InProcessBackend(block)
    if choice is not None:
        raise BrainError(f"brain.backend must be one of {', '.join(BACKENDS)}")
    if "command" not in block and brain_inprocess.available():
        return brain_inprocess.InProcessBackend(block)
    return MCPBackend(block)


class BrainClient:
    """The one face callers see: vault filtering on top of a backend."""

    def __init__(self, block: dict[str, Any], backend: Backend | None = None):
        self._backend: Backend = backend if backend is not None else backend_for(block)
        self._warnings_lock = threading.Lock()
        self._warnings: list[str] = []
        expanded = _expand_obj(block, dict(os.environ))
        self.agent_id = str(expanded.get("agent_id") or "") if isinstance(expanded, dict) else ""

    @property
    def backend(self) -> Backend:
        """The backend this client delegates to."""
        return self._backend

    @property
    def backend_name(self) -> str:
        """``mcp`` or ``inprocess``."""
        return "mcp" if isinstance(self._backend, MCPBackend) else "inprocess"

    @property
    def last_warnings(self) -> list[str]:
        """Degradations the backend reported on the most recent search."""
        with self._warnings_lock:
            return list(self._warnings)

    @property
    def _client(self) -> MCPClient | None:
        """The MCP child connection, when the backend has one."""
        return getattr(self._backend, "_client", None)

    def search(self, query: str, *, vault: str | None = None, top_k: int = 10) -> list[Hit]:
        """Hybrid search. A vault filter over-fetches, then keeps that vault's hits."""
        with self._warnings_lock:
            self._warnings = []
        notes: list[str] = []
        try:
            hits = self._backend.search(query, top_k=top_k * 4 if vault else top_k, warnings=notes)
        finally:
            with self._warnings_lock:
                self._warnings = notes
        if vault:
            hits = [h for h in hits if h.vault == vault]
        return hits[:top_k]

    def page(self, path: str) -> dict[str, Any]:
        """One note: {path, sensitivity, content}."""
        return self._backend.page(path)

    def age_hours(self) -> float | None:
        """Hours since the last full index run; None when the backend cannot say."""
        return self._backend.age_hours()

    def probe(self) -> float | None:
        """Raise BrainError unless the index answers for this identity; return its age."""
        return self._backend.probe()

    def clusters(self, max_tier: int) -> dict[str, Any] | None:
        """The topic map at or under ``max_tier``; None when the backend has none."""
        fn = getattr(self._backend, "clusters", None)
        return fn(max_tier) if callable(fn) else None

    def query_clusters(self, query: str, max_tier: int) -> dict[str, int] | None:
        """{cluster id: hit count} for ``query``; None when the backend has no map."""
        fn = getattr(self._backend, "query_clusters", None)
        if not callable(fn):
            return None
        notes: list[str] = []
        try:
            return fn(query, max_tier, warnings=notes)
        finally:
            with self._warnings_lock:
                self._warnings = notes

    def upgrade_pending(self) -> bool:
        """True while the index re-embeds after an upgrade; MCP cannot say, so False."""
        check = getattr(self._backend, "upgrade_pending", None)
        return bool(check()) if callable(check) else False

    def close(self) -> None:
        """Release the backend."""
        self._backend.close()


_CLIENTS: dict[str, BrainClient] = {}  # GLOBAL-STATE: one index process per config, shared by sessions
_CLIENTS_LOCK = threading.Lock()


def client_for(block: dict[str, Any]) -> BrainClient | None:
    """The shared client for a settings block, or None when no brain is configured."""
    if not block:
        return None
    key = json.dumps(block, sort_keys=True)
    with _CLIENTS_LOCK:
        if key not in _CLIENTS:
            _CLIENTS[key] = BrainClient(block)
        return _CLIENTS[key]
