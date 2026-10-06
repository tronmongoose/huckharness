"""Index staleness: recall, the Brain tool and the GUI status at 1 h, 40 h and 200 h."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("numpy")
pytest.importorskip("slos_recall")

from coding_harness.context import brain, recall  # noqa: E402
from coding_harness.modes import serve_gui  # noqa: E402
from coding_harness.tests import brain_db  # noqa: E402
from coding_harness.tools.brain import Brain  # noqa: E402

PROMPT = "Larkspur launch plan for the week"


class _Session:
    """The slice of Session that recall.prime touches."""

    def __init__(self, client: brain.BrainClient):
        self.registry = SimpleNamespace(tools={"Brain": Brain(client)})
        self.pinned: list[dict[str, Any]] = []
        self.recall_hints = False
        self._user_turn = 1
        self.logged: list[tuple[str, dict]] = []
        self.emitted: list[tuple[str, dict]] = []

    def _log(self, kind: str, payload: dict) -> None:
        self.logged.append((kind, payload))

    def _emit(self, kind: str, payload: dict) -> None:
        self.emitted.append((kind, payload))

    def mark_sensitive(self, source: str, tier: int) -> None:
        pass


def _client(tmp_path, monkeypatch, age: float | None) -> brain.BrainClient:
    return brain.BrainClient(brain_db.block(tmp_path, monkeypatch, age_hours=age))


def _prime(client: brain.BrainClient) -> tuple[str, _Session]:
    session = _Session(client)
    return recall.prime(session, PROMPT, cwd="/nonexistent"), session


def test_a_fresh_index_recalls_and_says_its_age(tmp_path, monkeypatch) -> None:
    block, session = _prime(_client(tmp_path, monkeypatch, 1.0))
    assert "startup/plan.md" in block
    primed = dict(session.emitted)["brain_primed"]
    assert primed["index_age_hours"] == pytest.approx(1.0, abs=0.1) and primed["stale"] is False


def test_a_day_and_a_half_old_index_recalls_but_is_marked_stale(tmp_path, monkeypatch) -> None:
    block, session = _prime(_client(tmp_path, monkeypatch, 40.0))
    assert "startup/plan.md" in block
    primed = dict(session.emitted)["brain_primed"]
    assert primed["stale"] is True and primed["index_age_hours"] == pytest.approx(40.0, abs=0.1)


def test_a_week_old_index_feeds_nothing_and_logs_once(tmp_path, monkeypatch) -> None:
    block, session = _prime(_client(tmp_path, monkeypatch, 200.0))
    assert block == "" and not session.emitted
    skipped = [p for k, p in session.logged if k == "recall_skipped_stale"]
    assert len(skipped) == 1 and skipped[0]["age_hours"] == pytest.approx(200.0, abs=0.1)


def test_an_unknown_age_never_skips(tmp_path, monkeypatch) -> None:
    block, session = _prime(_client(tmp_path, monkeypatch, None))
    assert "startup/plan.md" in block
    assert dict(session.emitted)["brain_primed"]["index_age_hours"] is None


@pytest.mark.parametrize(("age", "noted"), [(1.0, False), (40.0, True), (200.0, True)])
def test_the_brain_tool_answers_and_notes_a_stale_index(tmp_path, monkeypatch, age, noted) -> None:
    result = Brain(_client(tmp_path, monkeypatch, age)).run({"query": "launch"})
    assert "startup/plan.md" in result.content
    first = result.content.splitlines()[0]
    assert (first == f"Note: the index is {round(age)} hours old.") is noted


@pytest.mark.parametrize(("age", "stale"), [(1.0, False), (40.0, True), (200.0, True)])
def test_gui_status_reports_backend_and_age(tmp_path, monkeypatch, age, stale) -> None:
    status = serve_gui.brain_status(_client(tmp_path, monkeypatch, age))
    assert status["configured"] and status["ok"] and status["backend"] == "inprocess"
    assert status["age_hours"] == pytest.approx(age, abs=0.1) and status["stale"] is stale
    assert status["warnings"] == []


def test_gui_status_on_a_missing_index_is_not_ok(tmp_path, monkeypatch) -> None:
    block = brain_db.block(tmp_path, monkeypatch)
    status = serve_gui.brain_status(brain.BrainClient({**block, "db": str(tmp_path / "gone.db")}))
    assert status["ok"] is False and status["error"] == "index database not found"


def test_gui_status_for_an_unknown_identity_is_not_ok(tmp_path, monkeypatch) -> None:
    block = brain_db.block(tmp_path, monkeypatch)
    status = serve_gui.brain_status(brain.BrainClient({**block, "agent_id": "stranger"}))
    assert status["ok"] is False and status["error"] == "unknown brain identity"
