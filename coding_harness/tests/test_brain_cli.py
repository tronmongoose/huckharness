"""``bjorn brain`` status, search and index against a temp index."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("numpy")
pytest.importorskip("slos_recall")

from coding_harness import cli  # noqa: E402
from coding_harness.core.settings import Settings  # noqa: E402
from coding_harness.tests import brain_db, fake_brain  # noqa: E402


def _use(monkeypatch, block: dict | None) -> None:
    monkeypatch.setattr(cli, "_settings", lambda: Settings(brain=block or {}))


@pytest.fixture()
def block(tmp_path, monkeypatch):
    b = brain_db.block(tmp_path, monkeypatch)
    _use(monkeypatch, b)
    return b


def _mark_pruned(db) -> None:
    from slos_recall import store

    con = store.open_db(db)
    store.set_meta(con, "last_prune_at", str(time.time()))
    con.commit()
    con.close()


def test_search_prints_rank_tier_and_path_but_no_snippet(block, capsys) -> None:
    assert cli.main(["brain", "search", "larkspur", "--top-k", "3"]) == 0
    out = capsys.readouterr().out
    assert "finance/budget.md" in out and "confidential" in out
    assert out.splitlines()[0].split()[0] == "1"
    for _, text in fake_brain.NOTES.values():
        assert text not in out
    assert "1200" not in out and "Larkspur" not in out


def test_search_show_prints_snippets(block, capsys) -> None:
    assert cli._run_brain(["search", "budget", "--show", "--vault", "finance"]) == 0
    assert "1200 a month" in capsys.readouterr().out


def test_status_fresh_stale_and_unpruned(block, capsys, monkeypatch) -> None:
    assert cli._run_brain(["status"]) == 1  # no prune recorded yet
    out = capsys.readouterr().out
    assert "backend: inprocess" in out and "db: recall.db" in out and "prune" in out
    _mark_pruned(block["db"])
    assert cli._run_brain(["status"]) == 0
    assert "ok: yes" in capsys.readouterr().out
    brain_db.set_age(block["db"], 40.0)
    assert cli._run_brain(["status"]) == 1
    assert "last index run 40h ago" in capsys.readouterr().out


def test_status_on_mcp_prints_what_it_can(tmp_path, monkeypatch, capsys) -> None:
    _use(monkeypatch, fake_brain.block(tmp_path))
    assert cli._run_brain(["status"]) == 0
    out = capsys.readouterr().out
    assert "backend: mcp" in out and "unknown" in out


def test_misconfigured_exits_two(tmp_path, monkeypatch, capsys) -> None:
    _use(monkeypatch, None)
    assert cli._run_brain(["status"]) == 2
    _use(monkeypatch, {"backend": "inprocess"})
    assert cli._run_brain(["search", "x"]) == 2
    assert "agent_id" in capsys.readouterr().err


def test_index_refuses_on_mcp(tmp_path, monkeypatch, capsys) -> None:
    _use(monkeypatch, fake_brain.block(tmp_path))
    assert cli._run_brain(["index"]) == 2
    assert "in-process only" in capsys.readouterr().err


def test_index_one_file_then_find_it(block, tmp_path, capsys) -> None:
    note = tmp_path / "notes" / "startup" / "pricing.md"
    note.write_text("---\nsensitivity: internal\n---\nZanzibar pricing tiers for the launch.\n")
    assert cli._run_brain(["index", "--file", str(note)]) == 0
    assert "outcome:" in capsys.readouterr().out
    assert cli._run_brain(["search", "zanzibar"]) == 0
    assert "startup/pricing.md" in capsys.readouterr().out


def test_full_index_prints_counts(block, capsys) -> None:
    assert cli._run_brain(["index"]) == 0
    out = capsys.readouterr().out
    assert "indexed_pages:" in out and "new_chunks:" in out


def test_status_for_an_unknown_identity_exits_two(block, monkeypatch, capsys) -> None:
    _use(monkeypatch, {**block, "agent_id": "stranger"})
    assert cli._run_brain(["status"]) == 2
    assert "unknown brain identity" in capsys.readouterr().err
