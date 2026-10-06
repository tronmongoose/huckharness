"""A temp slos-recall index of fake_brain's four notes, for in-process brain tests.

``block(tmp, monkeypatch, age_hours=1.0)`` builds the DB, the note files the
library re-reads on every hit, and an identity table, then returns a settings
``brain`` block for the in-process backend. Every path is under ``tmp``: the
library's tier roots and its DB and identity defaults are re-pointed there,
so nothing touches the operator's index, and the query embedder is the
library's offline hash embedder. Callers importorskip slos_recall and numpy
first.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from coding_harness.tests.fake_brain import NOTES

AGENTS = """agents:
  bjorn-harness:
    clearance_level: 3
  low-agent:
    clearance_level: 1
"""


def _label(sensitivity: str) -> str:
    """The library stores no unlabelled page; the harness reads "" as restricted."""
    return sensitivity or "restricted"


def _write_notes(root: Path) -> None:
    """The note files, labelled in frontmatter as the indexer would read them."""
    for path, (sensitivity, text) in NOTES.items():
        f = root / path
        f.parent.mkdir(parents=True, exist_ok=True)
        head = f"---\nsensitivity: {sensitivity}\n---\n" if sensitivity else ""
        f.write_text(head + text + "\n", encoding="utf-8")


def set_age(db: Path, age_hours: float | None) -> None:
    """Stamp the last full index run ``age_hours`` ago; None clears it."""
    from slos_recall import store

    con = store.open_db(db)
    try:
        if age_hours is None:
            con.execute("DELETE FROM meta WHERE key = 'last_full_index_at'")
        else:
            store.set_meta(con, "last_full_index_at", str(time.time() - age_hours * 3600))
        con.commit()
    finally:
        con.close()


def block(tmp: Path, monkeypatch: Any, age_hours: float | None = 1.0) -> dict[str, Any]:
    """Build the index under ``tmp`` and return the matching in-process block."""
    from slos_recall import embed, index, testing

    monkeypatch.setattr(embed, "embed_one", lambda text, **_: testing.hash_embed(text))  # never Ollama
    monkeypatch.setattr(embed, "embed_batch", lambda texts, **_: [testing.hash_embed(t) for t in texts])
    root, db, agents = tmp / "notes", tmp / "recall.db", tmp / "agents.yaml"
    root.mkdir(parents=True, exist_ok=True)
    _write_notes(root)
    monkeypatch.setattr(index, "TIER_SPECS", ((root, "", "internal"),))
    monkeypatch.setenv("SLOS_RECALL_DB", str(tmp / "unused-default.db"))
    monkeypatch.setenv("SLOS_RECALL_AGENTS_YAML", str(tmp / "unused-agents.yaml"))
    agents.write_text(AGENTS, encoding="utf-8")
    pages = {p: (_label(s), t) for p, (s, t) in NOTES.items()}
    testing.build_db(db, pages).close()
    set_age(db, age_hours)
    return {"backend": "inprocess", "agent_id": "bjorn-harness",
            "db": str(db), "agents_yaml": str(agents)}
