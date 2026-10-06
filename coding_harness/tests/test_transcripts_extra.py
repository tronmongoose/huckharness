"""History management: search, the title sidecar, delete and the serve routes over them."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from coding_harness.core import paths, transcripts
from coding_harness.core import session as session_mod
from coding_harness.modes import serve_history

CWD = "/work/history-test"


def _write(sid: str, prompt: str = "fix the parser", *, cwd: str = CWD,
           envelope: dict | None = None, results: bool = True) -> Path:
    """A minimal transcript with one turn and one answered tool call."""
    rows = [
        {"ts": "2026-01-02T03:04:05Z", "kind": "session_start", "session_id": sid,
         "cwd": cwd, "envelope": envelope},
        {"kind": "user_message", "content": prompt, "turn": 1},
        {"kind": "assistant_message", "turn": 1, "content": "",
         "tool_calls": [{"id": "c1", "function": {"name": "Bash", "arguments": "{}"}}]},
    ]
    if results:
        rows.append({"kind": "tool_result", "turn": 1, "tool_call_id": "c1", "content": "ok"})
    rows.append({"kind": "user_message", "content": "and the lexer", "turn": 2})
    path = session_mod.SESSIONS_DIR / f"{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return path


@pytest.fixture(autouse=True)
def _clean_sessions():
    """Each test sees only its own transcripts."""
    yield
    for p in session_mod.SESSIONS_DIR.glob("hx-*"):
        os.remove(p)


def test_summary_counts_turns_and_flags_resumability() -> None:
    info = transcripts.summary(_write("hx-a"))
    assert info["turns"] == 2 and info["started"] == "2026-01-02T03:04:05Z"
    assert info["resumable"] is True and info["reason_if_not"] is None
    assert info["first_prompt"] == "fix the parser" and info["bytes"] > 0


def test_incomplete_and_expired_are_reported_with_resume_reason_codes() -> None:
    assert transcripts.summary(_write("hx-b", results=False))["reason_if_not"] == \
        "transcript_incomplete"
    dead = {"identity": "x", "grants": [], "revoked": True}
    info = transcripts.summary(_write("hx-c", envelope=dead))
    assert info["resumable"] is False and info["reason_if_not"] == "envelope_expired"


def test_search_matches_first_prompt_and_custom_title() -> None:
    _write("hx-d", "fix the parser")
    _write("hx-e", "write the docs")
    assert [p.stem for p in transcripts.search(CWD, "PARSER", 10)] == ["hx-d"]
    transcripts.set_title("hx-e", "release notes")
    assert [p.stem for p in transcripts.search(CWD, "release", 10)] == ["hx-e"]
    assert transcripts.search("/elsewhere", "parser", 10) == []


def test_title_drops_control_and_format_characters() -> None:
    _write("hx-m")
    assert transcripts.set_title("hx-m", "a\u202eb\x07c\u200bd") == "abcd"


def test_blank_first_prompt_is_not_listed() -> None:
    _write("hx-n", "   \n  ")
    ids = [r["id"] for r in serve_history.list_history(CWD, set())["transcripts"]]
    assert "hx-n" not in ids


def test_title_sidecar_overrides_and_clears() -> None:
    _write("hx-f")
    assert transcripts.set_title("hx-f", "  my   name ") == "my name"
    rows = serve_history.list_history(CWD, set())["transcripts"]
    row = next(r for r in rows if r["id"] == "hx-f")
    assert row["title"] == "my name" and row["first_prompt"] == "fix the parser"
    assert transcripts.set_title("hx-f", "") is None
    assert not (session_mod.SESSIONS_DIR / "hx-f.title").exists()
    assert transcripts.custom_title("hx-f") is None


def test_title_limits_and_unknown_ids() -> None:
    _write("hx-g")
    assert serve_history.post_transcript("hx-g/title", {"title": "x" * 121}, set(), CWD)[0] == 400
    assert serve_history.post_transcript("hx-none/title", {"title": "a"}, set(), CWD)[0] == 404
    assert serve_history.post_transcript("hx-g/title", {"title": 3}, set(), CWD)[0] == 400


def _session_trees(sid: str) -> list[Path]:
    """Checkpoint, shadow and proposal files for ``sid``, a nested dir, a link and plan files."""
    meta = paths.meta_dir()
    made = []
    for area in ("checkpoints", "shadow", "memory_proposals"):
        nested = meta / area / sid / "turn_0001"
        nested.mkdir(parents=True)
        (nested / "manifest.json").write_text("{}")
        made.append(nested / "manifest.json")
    outside = meta / "outside-target"
    outside.mkdir(exist_ok=True)
    (outside / "keep.txt").write_text("keep")
    os.symlink(outside, meta / "shadow" / sid / "link")
    (meta / "plans").mkdir(exist_ok=True)
    for name in (f"{sid}.md", f"{sid}.todo.json"):
        (meta / "plans" / name).write_text("x")
        made.append(meta / "plans" / name)
    return made


def test_delete_removes_exactly_the_planned_paths() -> None:
    path = _write("hx-h")
    transcripts.set_title("hx-h", "doomed")
    _session_trees("hx-h")
    neighbour = _write("hx-i")
    plan = transcripts.removal_plan("hx-h")
    names = {p.relative_to(paths.meta_dir()).as_posix() for p in plan}
    assert {"sessions/hx-h.jsonl", "sessions/hx-h.title", "checkpoints/hx-h",
            "checkpoints/hx-h/turn_0001/manifest.json", "shadow/hx-h/link",
            "plans/hx-h.md", "plans/hx-h.todo.json", "memory_proposals/hx-h",
            "memory_proposals/hx-h/turn_0001/manifest.json"} <= names
    (paths.meta_dir() / "plans" / "hx-i.md").write_text("keep")
    status, body = serve_history.post_transcript("hx-h/delete", {}, set(), CWD)
    assert status == 200 and set(body["removed"]) == names
    assert not any(p.exists() or p.is_symlink() for p in plan)
    assert not path.exists() and neighbour.exists()
    assert (paths.meta_dir() / "plans" / "hx-i.md").exists()
    assert (paths.meta_dir() / "outside-target" / "keep.txt").exists(), "links are not followed"


def test_delete_refuses_live_bad_and_unknown_ids() -> None:
    path = _write("hx-j")
    assert serve_history.post_transcript("hx-j/delete", {}, {"hx-j"}, CWD)[0] == 409
    assert path.exists()
    assert serve_history.post_transcript("..%2Fx/delete", {}, set(), CWD)[0] == 400
    assert serve_history.post_transcript("../hx-j/delete", {}, set(), CWD)[0] == 400
    assert serve_history.post_transcript("hx-nope/delete", {}, set(), CWD)[0] == 404
    with pytest.raises(transcripts.TranscriptError):
        transcripts.delete("a/b", set(), CWD)


def test_list_history_filters_with_q_and_hides_live() -> None:
    _write("hx-k", "fix the parser")
    _write("hx-l", "write the docs")
    ids = [r["id"] for r in serve_history.list_history(CWD, {"hx-l"}, "")["transcripts"]]
    assert "hx-k" in ids and "hx-l" not in ids
    ids = [r["id"] for r in serve_history.list_history(CWD, set(), "docs")["transcripts"]]
    assert ids == ["hx-l"]


def test_delete_refuses_another_projects_session() -> None:
    path = _write("hx-o", cwd="/work/other-project")
    status, body = serve_history.post_transcript("hx-o/delete", {}, set(), CWD)
    assert status == 409 and "another project" in body["error"]["message"]
    assert path.exists()


def test_a_stuck_tree_fails_loudly_and_keeps_the_transcript() -> None:
    path = _write("hx-p")
    made = _session_trees("hx-p")
    stuck = made[0].parent  # checkpoints/hx-p/turn_0001
    os.chmod(stuck, 0o555)
    try:
        status, body = serve_history.post_transcript("hx-p/delete", {}, set(), CWD)
    finally:
        os.chmod(stuck, 0o755)
    assert status == 500
    assert body["failed"] == "checkpoints/hx-p/turn_0001/manifest.json"
    assert body["removed"] == [] and path.exists()
    status, body = serve_history.post_transcript("hx-p/delete", {}, set(), CWD)
    assert status == 200 and not path.exists(), "a retry finishes the job"
