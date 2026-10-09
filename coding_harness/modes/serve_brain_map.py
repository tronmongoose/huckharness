"""Topic-map routes for the Brain tab. Screen only.

Routes:
  GET /v1/brain/map?max_tier=            -> {available, max_tier, pages, clusters, central}
                                            or {available: false, reason}
  GET /v1/brain/map/search?q=&max_tier=  -> {available, hits: {cluster id: count}, warnings}

``max_tier`` is 0 to 3 and defaults to 3. The backend narrows it further to
the identity's clearance, and the answer names the tier actually used.

Nothing here reaches a session: no history, no transcript, no event, no
model context. The map and its search are for the person at the screen,
as ``/v1/brain/search`` is. serve_mode only dispatches here.

Exports: PATHS, OPENAPI_PATHS, handle().
"""
from __future__ import annotations

from typing import Any

from coding_harness.context.brain import BrainError
from coding_harness.context.brain_clusters import MAP_UNAVAILABLE

PATHS = frozenset({"/v1/brain/map", "/v1/brain/map/search"})
MAX_QUERY = 500
DEFAULT_TIER = 3

_TIER = {"name": "max_tier", "in": "query", "schema": {"type": "integer", "minimum": 0,
                                                       "maximum": 3}}
OPENAPI_PATHS: dict[str, Any] = {
    "/v1/brain/map": {"get": {
        "summary": "Topic clusters of the brain at or under max_tier, for the screen",
        "parameters": [_TIER], "responses": {"200": {"description": "clusters"}}}},
    "/v1/brain/map/search": {"get": {
        "summary": "Search hits per topic cluster: {hits: {cluster id: count}}",
        "parameters": [{"name": "q", "in": "query", "schema": {"type": "string"}}, _TIER],
        "responses": {"200": {"description": "hit counts"}}}},
}


def parse_tier(raw: str | None) -> int | None:
    """``max_tier`` as 0..3; missing means 3, anything malformed is None."""
    if raw is None or raw == "":
        return DEFAULT_TIER
    try:
        tier = int(raw)
    except ValueError:
        return None
    return tier if 0 <= tier <= 3 else None


def _first(query: dict[str, list[str]], name: str) -> str | None:
    """The first value of one query parameter."""
    values = query.get(name)
    return values[0] if values else None


def handle(h: Any, client: Any, path: str, query: dict[str, list[str]]) -> None:
    """Answer one map route; ``client`` is the shared brain client or None."""
    if client is None:
        h._send_error_json(404, "no second brain configured")
        return
    tier = parse_tier(_first(query, "max_tier"))
    if tier is None:
        h._send_error_json(400, "max_tier must be an integer from 0 to 3")
        return
    try:
        if path == "/v1/brain/map":
            out = client.clusters(tier)
            h._send_json(200, out if out is not None
                         else {"available": False, "reason": MAP_UNAVAILABLE})
            return
        q = (_first(query, "q") or "").strip()
        if not q or len(q) > MAX_QUERY:
            h._send_error_json(400, f"q is required, at most {MAX_QUERY} characters")
            return
        hits = client.query_clusters(q, tier)
        if hits is None:
            h._send_json(200, {"available": False, "reason": MAP_UNAVAILABLE})
            return
        h._send_json(200, {"available": True, "hits": hits, "warnings": client.last_warnings})
    except BrainError as e:
        h._send_error_json(502, str(e))
