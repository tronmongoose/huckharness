"""The second brain read in-process through the slos_recall library.

Exports ``available``, ``InProcessBackend``, ``EMBED_TIMEOUT_S`` and
``UPGRADE_WARNING``.

slos_recall (numpy, pyyaml) is optional. Nothing here imports it at module
load: every library module is fetched with importlib inside the call that
needs it, so the harness imports and runs without the package. The library
gates every result on the caller's clearance, read per call from its
identity table; the harness's own ``allowed_tier`` caps still apply on top,
in the callers, exactly as for the MCP backend.

The query is embedded with ``api.query_embedder``: the index's stamped model
and query prefix, so a query never lands in a different embedding space
from the stored vectors. While an upgrade's re-embed is pending the library
keeps vectors off, so the search goes keyword-only without embedding at all.

Errors leave as ``BrainError`` with absolute paths reduced to basenames, since
messages reach the GUI and the model.
"""
from __future__ import annotations

import contextlib
import importlib
import importlib.util
import os
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Callable

from coding_harness.context.brain import BrainError, Hit
from coding_harness.mcp.config import _expand_obj

EMBED_TIMEOUT_S = 5.0
EMBED_COOLDOWN_S = 60.0  # after a timeout, Ollama is presumed cold or gone for this long
UPGRADE_WARNING = "index upgrade in progress, keyword search only"
_LIB_VECTORS_OFF = "vector search off until the upgrade"
_QUOTED_PATH = re.compile(r"(['\"])(~?/[^'\"\n]*)\1")
_ABS_PATH = re.compile(r"(?<![\w.-])~?(?:/[^/\s'\"]+)+/?")


def available() -> bool:
    """True when the slos_recall package can be imported."""
    try:
        return importlib.util.find_spec("slos_recall") is not None
    except (ImportError, ValueError):
        return False


def _lib(name: str) -> Any:
    """``slos_recall.<name>``, imported on first use."""
    try:
        return importlib.import_module(f"slos_recall.{name}")
    except ImportError as e:
        raise BrainError(f"slos_recall is not importable ({e.name or 'missing module'})") from e


def _base(path: str) -> str:
    """The last segment of ``path``, never empty."""
    return Path(path.rstrip()).name or "/"


def _scrub(message: str) -> str:
    """``message`` with every absolute path cut to its basename.

    A quoted path may hold spaces, so it goes whole. An unquoted path under a
    home directory may too, so it runs to the next quote or the end of the
    line. Any other absolute path ends at whitespace.
    """
    message = _QUOTED_PATH.sub(lambda m: m.group(1) + _base(m.group(2)) + m.group(1), message)
    homes = [re.escape(os.path.expanduser("~")), "/Users/", "/home/"]
    home_path = re.compile(r"(?<![\w.-])(?:" + "|".join(homes) + r")[^'\"\n]*")
    message = home_path.sub(lambda m: _base(m.group(0)), message)
    return _ABS_PATH.sub(lambda m: _base(m.group(0)), message)


def _timeout(value: Any) -> float:
    """The embed budget from the block; a bad value is a configuration error."""
    if value is None:
        return EMBED_TIMEOUT_S
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise BrainError("brain.embed_timeout_s must be a positive number")
    return float(value)


def _upgrade_notes(notes: list[str]) -> list[str]:
    """``notes`` with the library's vectors-off line said as an upgrade in progress, once."""
    out = [UPGRADE_WARNING if str(n).startswith(_LIB_VECTORS_OFF) else n for n in notes]
    return [n for i, n in enumerate(out) if n != UPGRADE_WARNING or out.index(n) == i]


class InProcessBackend:
    """Search and read the index with the library, one read-only connection per call."""

    def __init__(self, block: dict[str, Any],
                 embed_fn: Callable[[str], Any] | None = None):
        if not available():
            raise BrainError("brain.backend is inprocess but slos_recall is not importable")
        expanded = _expand_obj(block, dict(os.environ))
        assert isinstance(expanded, dict)
        self.agent_id = str(expanded.get("agent_id") or "")
        if not self.agent_id:
            raise BrainError("brain.agent_id is required for the in-process backend")
        db, agents = expanded.get("db"), expanded.get("agents_yaml")
        self._db = os.path.expanduser(str(db)) if db else None
        self._agents_yaml = os.path.expanduser(str(agents)) if agents else None
        self.embed_timeout_s = _timeout(expanded.get("embed_timeout_s"))
        self.embed_fn = embed_fn  # tests inject a fake; None means the index's stamped embedder
        # Guards only the embed slot and the cooldown; DB work takes no lock.
        self._state = threading.Lock()
        self._embedding = False
        self._cooldown_until = 0.0

    @contextlib.contextmanager
    def _guard(self) -> Iterator[None]:
        """Turn any library failure into a BrainError that names no absolute path."""
        try:
            yield
        except BrainError:
            raise
        except FileNotFoundError as e:
            raise BrainError("index database not found") from e
        except Exception as e:  # noqa: BLE001 the library is a trust boundary; nothing else may leak
            raise BrainError(_scrub(f"index error: {type(e).__name__}: {e}")) from e

    @contextlib.contextmanager
    def _connection(self) -> Iterator[Any]:
        """A read-only connection of this call's own; SQLite readers need no lock."""
        con = _lib("store").open_ro(self._db)
        try:
            yield con
        finally:
            con.close()

    def _clearance(self) -> int:
        """The agent's clearance from the identity table, read fresh each call."""
        identity = _lib("identity")
        level = identity.agent_clearance(self.agent_id, identity.load_agents(self._agents_yaml))
        if level is None:
            raise BrainError("unknown brain identity")
        return int(level)

    def db_path(self) -> Path:
        """The index file this backend reads."""
        return Path(self._db) if self._db else Path(_lib("store").default_db())

    def _claim_embed(self) -> str | None:
        """None when this call may embed; otherwise why it goes keyword-only."""
        with self._state:
            if time.monotonic() < self._cooldown_until:
                return "embedding timed out recently; keyword search only during the cooldown"
            if self._embedding:
                return "another query embedding is in flight; keyword search only"
            self._embedding = True
            return None

    def _release_embed(self, timed_out: bool) -> None:
        """Free the embed slot; a timeout starts the cooldown, a success clears it."""
        with self._state:
            if timed_out:
                self._cooldown_until = time.monotonic() + EMBED_COOLDOWN_S
            else:
                self._embedding = False
                self._cooldown_until = 0.0

    def _query_embedder(self) -> tuple[Callable[[str], Any] | None, bool]:
        """(embedder, upgrade pending), read on a connection closed before any embed.

        ``query_embedder`` resolves the stamps while the connection is open
        and binds plain values, so the embedder outlives it. None when
        pending: the library would ignore the vector.
        """
        with self._connection() as con:
            if _lib("versions").reembed_pending(con):
                return None, True
            return self.embed_fn or _lib("api").query_embedder(con), False

    def upgrade_pending(self) -> bool:
        """True while an index upgrade's re-embed is unfinished: keyword search only."""
        with self._guard(), self._connection() as con:
            return bool(_lib("versions").reembed_pending(con))

    def _embed(self, query: str, warnings: list[str]) -> Any | None:
        """The query vector, or None after noting why the search is keyword-only.

        At most one embedding runs at a time. One that outlives the budget
        keeps the slot until it finishes, so stranded requests never pile up.
        """
        base, pending = self._query_embedder()
        if pending or base is None:
            warnings.append(UPGRADE_WARNING)
            return None
        busy = self._claim_embed()
        if busy:
            warnings.append(busy)
            return None
        box: dict[str, Any] = {}

        def work() -> None:
            try:
                box["vec"] = base(query)
            except Exception as e:  # noqa: BLE001 any embed fault means keyword search
                box["err"] = e
            finally:
                with self._state:
                    self._embedding = False

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(self.embed_timeout_s)
        if worker.is_alive():
            self._release_embed(timed_out=True)
            warnings.append(f"embedding timed out after {self.embed_timeout_s:g}s; keyword "
                            f"search only for the next {EMBED_COOLDOWN_S:g}s")
            return None
        if "err" in box:
            warnings.append(f"embedding failed: {type(box['err']).__name__}; keyword search only")
            return None
        self._release_embed(timed_out=False)
        return box["vec"]

    def search(self, query: str, *, top_k: int,
               warnings: list[str] | None = None) -> list[Hit]:
        """Hits at or under the agent's clearance; degradations are appended to ``warnings``."""
        notes: list[str] = []
        try:
            with self._guard():
                clearance = self._clearance()
                vec = self._embed(query, notes)
                api = _lib("api")
                args = {"clearance": clearance, "query": query, "top_k": top_k, "warnings": notes}
                with self._connection() as con:
                    if vec is None:
                        rows = api.search(con, vector=False, **args)
                    else:
                        rows = api.search(con, embed_fn=lambda _text: vec, **args)
        finally:
            if warnings is not None:
                warnings.extend(_scrub(str(w)) for w in _upgrade_notes(notes))
        return [Hit(str(r.get("page_path")), str(r.get("sensitivity") or ""),
                    float(r.get("score") or 0.0), str(r.get("chunk_text") or ""))
                for r in rows]

    def page(self, path: str) -> dict[str, Any]:
        """One note; unseen, absent and over-clearance pages all answer "not found"."""
        api = _lib("api")
        with self._guard(), self._connection() as con:
            try:
                data = api.get_page(con, clearance=self._clearance(), path=path)
            except api.ApiError as e:
                raise BrainError("not found") from e
        return {"path": str(data.get("path") or path),
                "sensitivity": str(data.get("sensitivity") or ""),
                "content": str(data.get("content") or "")}

    def probe(self) -> float | None:
        """Prove this identity may read the index, without a search; return its age."""
        with self._guard():
            self._clearance()
        return self.age_hours()

    def age_hours(self) -> float | None:
        """Hours since the last full index run, or None when never recorded."""
        with self._guard(), self._connection() as con:
            return _lib("health").index_age_hours(con)

    def freshness(self) -> Any:
        """The library's four-check freshness verdict for this index."""
        with self._guard(), self._connection() as con:
            return _lib("health").freshness(con)

    def index(self, file: str | None = None) -> dict[str, Any]:
        """Run the library's indexer on this DB: one file, or a full pass with prune."""
        with self._guard():
            lib = _lib("index")
            if file:
                return lib.index_file(Path(file).expanduser(), db_path=self.db_path(),
                                      embed_fn=self.embed_fn)
            return lib.index_once(db_path=self.db_path(), embed_fn=self.embed_fn)

    def close(self) -> None:
        """Nothing is held between calls."""
