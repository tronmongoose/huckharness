"""serve_changes handlers against a stand-in session entry: locking and review state."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

from coding_harness.models import ollama
from coding_harness.modes import serve_changes


def _entry(diff_fn: Any) -> SimpleNamespace:
    """A session entry whose session.diff is ``diff_fn``."""
    return SimpleNamespace(
        lock=threading.Lock(), listeners_lock=threading.Lock(), turn_active=False,
        review_writes=True, broker=None, session=SimpleNamespace(diff=diff_fn),
    )


def test_diff_read_never_claims_turn_active() -> None:
    seen: list[bool] = []
    entry = _entry(lambda turn: seen.append(entry.turn_active) or {"turn": turn, "files": []})
    assert serve_changes.get_diff(entry, {"turn": ["2"]}) == (200, {"turn": 2, "files": []})
    assert seen == [False]


def test_overlapping_diff_reads_both_succeed() -> None:
    def slow(turn: Any) -> dict:
        time.sleep(0.2)
        return {"turn": turn, "files": []}

    entry = _entry(slow)
    results: list[int] = []
    threads = [
        threading.Thread(target=lambda: results.append(serve_changes.get_diff(entry, {})[0]))
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert results == [200, 200]


def test_diff_refused_while_turn_runs() -> None:
    entry = _entry(lambda turn: {})
    entry.turn_active = True
    assert serve_changes.get_diff(entry, {})[0] == 409


def test_annotate_resolved_settles_review() -> None:
    entry = _entry(None)
    once = serve_changes.annotate_resolved(
        entry, "permission_resolved", {"req_id": "a", "decision": "allow_once", "kind": "review"})
    assert once["review_writes"] is True
    always = serve_changes.annotate_resolved(
        entry, "permission_resolved", {"req_id": "b", "decision": "allow_always", "kind": "review"})
    assert always["review_writes"] is False and entry.review_writes is False
    plain = {"req_id": "c", "decision": "allow_always", "kind": "permission"}
    assert serve_changes.annotate_resolved(entry, "permission_resolved", plain) is plain


def test_ollama_wire_drops_private_keys() -> None:
    msgs = [{"role": "user", "content": "note", "_operator_note": True}]
    assert ollama._wire(msgs) == [{"role": "user", "content": "note"}]
    assert msgs[0]["_operator_note"] is True
