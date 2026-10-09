"""Topic-map routes: the tier cap, a backend without a map, and no session side effects."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from coding_harness.context import brain
from coding_harness.context.brain_clusters import MAP_UNAVAILABLE, shape_map
from coding_harness.core import paths
from coding_harness.core.session import Session
from coding_harness.core.settings import Settings
from coding_harness.modes import serve_brain_map
from coding_harness.security import audit
from coding_harness.tests.test_serve_brain import _serve
from coding_harness.tests.test_serve_mode import _fake_ollama_chat, _request

MARKER = "quillwort"
BLOCK = {"backend": "fake-map", "agent_id": "map-test"}
RAW = {
    "clusters": [
        {"id": 0, "label": f"{MARKER} garden", "size": 3, "vaults": {"hot": 3}, "max_tier": 1,
         "near": [[1, 0.8]]},
        {"id": 1, "label": "boat keel", "size": 2, "vaults": {"cold": 2}, "max_tier": 2,
         "near": [[0, 0.8]]},
    ],
    "pages": {"hot/a.md": 0, "hot/b.md": 0, "hot/c.md": 0, "cold/d.md": 1, "cold/e.md": 1},
    "central": {0: ["hot/a.md"], 1: ["cold/d.md"]},
    "titles": {"hot/a.md": "Garden", "cold/d.md": "Boat"},
}


class FakeMapBackend:
    """A backend with a map whose identity clearance is 2."""

    clearance = 2

    def __init__(self) -> None:
        self.tiers: list[int] = []

    def search(self, query: str, *, top_k: int, warnings: list[str] | None = None) -> list:
        return []

    def page(self, path: str) -> dict[str, Any]:
        raise brain.BrainError("not found")

    def age_hours(self) -> float | None:
        return None

    def probe(self) -> float | None:
        return None

    def close(self) -> None:
        pass

    def clusters(self, max_tier: int) -> dict[str, Any]:
        self.tiers.append(max_tier)
        return shape_map(RAW, min(max_tier, self.clearance))

    def query_clusters(self, query: str, max_tier: int,
                       warnings: list[str] | None = None) -> dict[str, int]:
        self.tiers.append(max_tier)
        if warnings is not None:
            warnings.append("keyword search only")
        return {"0": 2} if MARKER in query else {}


class NoMapBackend(FakeMapBackend):
    """A backend that predates the map, as the MCP one does."""

    clusters = None  # type: ignore[assignment]
    query_clusters = None  # type: ignore[assignment]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("map")
    patches = [
        mock.patch.object(audit, "AUDIT_PATH", tmp / "audit.jsonl"),
        mock.patch.object(audit, "ANCHORS_PATH", tmp / "anchors.jsonl"),
        mock.patch.object(audit, "META_DIR", tmp),
        mock.patch("coding_harness.core.session.ollama.chat", side_effect=_fake_ollama_chat),
    ]
    for p in patches:
        p.start()
    backend = FakeMapBackend()
    key = json.dumps(BLOCK, sort_keys=True)
    brain._CLIENTS[key] = brain.BrainClient(BLOCK, backend=backend)
    srv, thread = _serve(Settings(brain=BLOCK), brain=True)
    yield srv, backend, key, tmp
    srv.shutdown()
    srv.server_close()
    thread.join(timeout=2.0)
    brain._CLIENTS.pop(key, None)
    for p in patches:
        p.stop()


def _get(srv, path: str) -> tuple[int, Any]:
    status, body = _request("GET", f"http://127.0.0.1:{srv.server_port}{path}")
    return status, json.loads(body)


def test_map_defaults_to_tier_three_and_the_backend_narrows_it(server) -> None:
    srv, backend, _, _ = server
    backend.tiers.clear()
    status, out = _get(srv, "/v1/brain/map")
    assert status == 200 and backend.tiers == [3]
    assert out["available"] is True and out["max_tier"] == 2 and out["pages"] == 5
    assert out["central"]["0"] == [{"path": "hot/a.md", "title": "Garden"}]
    assert "hot/b.md" not in json.dumps(out)


def test_requested_tier_is_passed_down_and_bad_tiers_are_refused(server) -> None:
    srv, backend, _, _ = server
    backend.tiers.clear()
    assert _get(srv, "/v1/brain/map?max_tier=1")[1]["max_tier"] == 1
    assert _get(srv, f"/v1/brain/map/search?q={MARKER}&max_tier=0")[1]["hits"] == {"0": 2}
    assert backend.tiers == [1, 0]
    for bad in ("4", "-1", "two"):
        assert _get(srv, f"/v1/brain/map?max_tier={bad}")[0] == 400
    assert _get(srv, "/v1/brain/map/search?q=")[0] == 400
    assert _get(srv, "/v1/brain/map/search?q=" + "x" * 501)[0] == 400


def test_search_reports_backend_warnings(server) -> None:
    srv, _, _, _ = server
    status, out = _get(srv, f"/v1/brain/map/search?q={MARKER}")
    assert status == 200 and out["warnings"] == ["keyword search only"]


def test_a_backend_without_a_map_degrades(server) -> None:
    srv, _, key, _ = server
    original = brain._CLIENTS[key]
    brain._CLIENTS[key] = brain.BrainClient(BLOCK, backend=NoMapBackend())
    try:
        assert _get(srv, "/v1/brain/map") == (200, {"available": False, "reason": MAP_UNAVAILABLE})
        status, out = _get(srv, "/v1/brain/map/search?q=boat")
        assert status == 200 and out["available"] is False
    finally:
        brain._CLIENTS[key] = original


def test_no_brain_is_404(tmp_path) -> None:
    calls: list[tuple[int, Any]] = []
    h = mock.MagicMock()
    h._send_error_json.side_effect = lambda code, msg: calls.append((code, msg))
    serve_brain_map.handle(h, None, "/v1/brain/map", {})
    assert calls and calls[0][0] == 404


def _files_containing(root: Path, needle: str) -> list[str]:
    hits = []
    for dirpath, _, names in os.walk(root):
        for name in names:
            f = Path(dirpath) / name
            try:
                if needle in f.read_text(errors="ignore"):
                    hits.append(name)
            except OSError:
                continue
    return hits


def test_map_results_never_reach_a_session(server) -> None:
    srv, _, _, tmp = server
    seen: list[Session] = []
    original = Session._emit

    def spy(self, kind, payload):
        seen.append(self)
        return original(self, kind, payload)

    status, body = _request("POST", f"http://127.0.0.1:{srv.server_port}/v1/sessions", body={})
    assert status == 201, body
    with mock.patch.object(Session, "_emit", spy):
        _get(srv, "/v1/brain/map")
        _get(srv, f"/v1/brain/map/search?q={MARKER}")
    assert seen == []
    assert _files_containing(paths.meta_dir(), MARKER) == []
    assert _files_containing(tmp, MARKER) == []


def test_in_process_map_caps_at_the_identity_clearance(tmp_path, monkeypatch) -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("slos_recall")
    from slos_recall import api, testing

    if not hasattr(api, "clusters"):
        pytest.skip("slos_recall predates clusters")
    from coding_harness.context import brain_inprocess
    from coding_harness.tests import brain_db

    block = {**brain_db.block(tmp_path, monkeypatch), "agent_id": "low-agent"}
    backend = brain_inprocess.InProcessBackend(block, embed_fn=testing.hash_embed)
    client = brain.BrainClient(block, backend=backend)
    out = client.clusters(3)
    assert out is not None and out["max_tier"] == 1
    assert out["pages"] == 1 and all(c["max_tier"] <= 1 for c in out["clusters"])
    shown = [n["path"] for notes in out["central"].values() for n in notes]
    assert shown == ["startup/plan.md"]
    hits = client.query_clusters("larkspur launch", 3)
    assert hits == {"0": 1}
    assert client.clusters(0)["pages"] == 0
