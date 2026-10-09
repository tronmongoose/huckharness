"""Topic clusters of the second brain, for the Brain tab's map. Screen only.

Exports ``ClusterMixin``, ``MAP_UNAVAILABLE`` and ``shape_map``.

``ClusterMixin`` gives the in-process backend ``clusters(max_tier)`` and
``query_clusters(q, max_tier)``. Both run at the stricter of the requested
tier and the identity's clearance, so a caller can narrow what the map shows
but never widen it past the identity. The MCP backend has neither method,
and ``BrainClient`` reports the map unavailable for it.

Nothing here touches a session: the map is for the person at the screen,
never for a model, a transcript or session history.
"""
from __future__ import annotations

from typing import Any

MAP_UNAVAILABLE = "the topic map needs the in-process backend"


def shape_map(raw: dict[str, Any], max_tier: int) -> dict[str, Any]:
    """The library's clusters payload cut to what the screen draws.

    The per-page assignment is dropped: the screen needs only clusters and
    their central notes, not every path in the index.
    """
    titles = raw.get("titles") or {}
    central = {
        str(cid): [{"path": p, "title": str(titles.get(p) or "")} for p in paths]
        for cid, paths in (raw.get("central") or {}).items()
    }
    return {"available": True, "max_tier": max_tier,
            "pages": len(raw.get("pages") or {}),
            "clusters": list(raw.get("clusters") or []), "central": central}


class ClusterMixin:
    """Cluster calls for ``InProcessBackend``; relies on its guard, connection,
    clearance and embed helpers."""

    def _capped(self, max_tier: int) -> int:
        """The requested tier, never above the identity's clearance."""
        return min(int(max_tier), self._clearance())

    def clusters(self, max_tier: int) -> dict[str, Any]:
        """Clusters of the pages at or under the capped tier."""
        from coding_harness.context.brain_inprocess import _lib

        with self._guard():
            tier = self._capped(max_tier)
            with self._connection() as con:
                raw = _lib("api").clusters(con, tier)
        return shape_map(raw, tier)

    def query_clusters(self, query: str, max_tier: int,
                       warnings: list[str] | None = None) -> dict[str, int]:
        """{cluster id: hit count} for ``query`` at the capped tier."""
        from coding_harness.context.brain_inprocess import _lib, _scrub, _upgrade_notes

        notes: list[str] = []
        try:
            with self._guard():
                tier = self._capped(max_tier)
                vec = self._embed(query, notes)
                kw: dict[str, Any] = ({"vector": False} if vec is None
                                      else {"embed_fn": lambda _text: vec})
                with self._connection() as con:
                    counts = _lib("api").query_clusters(con, tier, query, warnings=notes, **kw)
        finally:
            if warnings is not None:
                warnings.extend(_scrub(str(w)) for w in _upgrade_notes(notes))
        return {str(cid): int(n) for cid, n in counts.items()}
