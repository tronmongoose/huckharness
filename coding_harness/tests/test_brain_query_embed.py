"""The in-process brain embeds queries with the index's stamped model and prefix.

Covers the library's query embedder seam, the short-lived stamp read, and the
re-embed-pending state surfacing as keyword-only search with a status flag.
"""
from __future__ import annotations

import pytest

pytest.importorskip("numpy")
pytest.importorskip("slos_recall")

from coding_harness.context import brain, brain_inprocess  # noqa: E402
from coding_harness.modes import serve_gui  # noqa: E402
from coding_harness.tests import brain_db  # noqa: E402


@pytest.fixture()
def block(tmp_path, monkeypatch):
    return brain_db.block(tmp_path, monkeypatch)


def _stamp(db: str, model: str | None, prefix: int | None = None, pending: bool = False) -> None:
    """Write embed_model / prefix_version stamps and the pending marker into ``db``."""
    from slos_recall import store, versions

    con = store.open_db(db)
    try:
        if model is not None:
            store.set_meta(con, "embed_model", model)
        if prefix is not None:
            store.set_meta(con, "prefix_version", str(prefix))
        versions.set_reembed_pending(con, pending)
        con.commit()
    finally:
        con.close()


def _record_embeds(monkeypatch) -> list[str]:
    """Replace the library's base embedder with one that records each text it gets."""
    from slos_recall import embed, testing

    seen: list[str] = []

    def recorder(text, **_):
        seen.append(text)
        return testing.hash_embed(text)

    monkeypatch.setattr(embed, "embed_one", recorder)
    return seen


def test_a_stamped_prefix_reaches_the_base_embedder(block, monkeypatch) -> None:
    _stamp(block["db"], "nomic-embed-text", prefix=1)
    seen = _record_embeds(monkeypatch)
    warnings: list[str] = []
    hits = brain_inprocess.InProcessBackend(block).search("budget", top_k=3, warnings=warnings)
    assert seen == ["search_query: budget"]
    assert [h.path for h in hits][:1] == ["finance/budget.md"] and warnings == []


def test_an_unstamped_index_sends_the_bare_query(block, monkeypatch) -> None:
    seen = _record_embeds(monkeypatch)
    brain_inprocess.InProcessBackend(block).search("budget", top_k=3)
    assert seen == ["budget"]


def test_the_connection_closes_before_the_embed_runs(block, monkeypatch) -> None:
    from slos_recall import store

    events: list[str] = []
    real_open = store.open_ro

    class Tracked:
        def __init__(self, con):
            self._con = con

        def __getattr__(self, name):
            return getattr(self._con, name)

        def close(self):
            events.append("close")
            self._con.close()

    def tracked_open(*args, **kwargs):
        events.append("open")
        return Tracked(real_open(*args, **kwargs))

    def embed_one(text, **_):
        events.append("embed")
        return _hash(text)

    monkeypatch.setattr(store, "open_ro", tracked_open)
    monkeypatch.setattr("slos_recall.embed.embed_one", embed_one)
    brain_inprocess.InProcessBackend(block).search("budget", top_k=3)
    first_embed = events.index("embed")
    assert events[:first_embed] == ["open", "close"]
    assert events[first_embed:] == ["embed", "open", "close"]


def _hash(text: str):
    """The library's offline hash vector."""
    from slos_recall import testing

    return testing.hash_embed(text)


def test_a_pending_reembed_is_keyword_only_and_never_embeds(block, monkeypatch) -> None:
    _stamp(block["db"], "nomic-embed-text", prefix=1, pending=True)
    seen = _record_embeds(monkeypatch)
    backend = brain_inprocess.InProcessBackend(block)
    client = brain.BrainClient(block, backend=backend)
    assert [h.path for h in client.search("budget")] == ["finance/budget.md"]
    assert seen == [] and client.last_warnings == [brain_inprocess.UPGRADE_WARNING]
    status = serve_gui.brain_status(client)
    assert status["ok"] and status["upgrade_pending"] is True
    assert brain_inprocess.UPGRADE_WARNING in status["warnings"]


def test_the_injected_embedder_still_sees_the_pending_state(block) -> None:
    _stamp(block["db"], None, pending=True)
    calls: list[str] = []
    backend = brain_inprocess.InProcessBackend(block, embed_fn=lambda t: calls.append(t) or _hash(t))
    warnings: list[str] = []
    backend.search("budget", top_k=3, warnings=warnings)
    assert calls == [] and warnings == [brain_inprocess.UPGRADE_WARNING]
    assert backend.upgrade_pending() is True


def test_the_library_vectors_off_note_reads_as_an_upgrade() -> None:
    notes = ["vector search off until the upgrade's re-embed finishes (run `x`)",
             brain_inprocess.UPGRADE_WARNING, "other"]
    assert brain_inprocess._upgrade_notes(notes) == [brain_inprocess.UPGRADE_WARNING, "other"]


def test_status_without_an_upgrade_reports_false(block) -> None:
    from slos_recall import testing

    backend = brain_inprocess.InProcessBackend(block, embed_fn=testing.hash_embed)
    status = serve_gui.brain_status(brain.BrainClient(block, backend=backend))
    assert status["upgrade_pending"] is False and status["warnings"] == []


def test_cli_status_says_the_upgrade_is_pending(block, capsys) -> None:
    from coding_harness.modes import brain_cmd

    _stamp(block["db"], None, pending=True)
    client = brain.BrainClient(block, backend=brain_inprocess.InProcessBackend(block))
    brain_cmd._status(client, None)
    assert f"upgrade pending: yes ({brain_inprocess.UPGRADE_WARNING})" in capsys.readouterr().out
