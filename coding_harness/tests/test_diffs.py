"""Snapshotter.forget and the manifest-based diff/revert fallback in core/diffs.py."""
from __future__ import annotations

import json
import os
from pathlib import Path

from coding_harness.core import diffs
from coding_harness.security.snapshot import Snapshotter
from coding_harness.tools.base import WritePlan


def _plan(path: Path, pre: bytes | None, post: bytes) -> WritePlan:
    """A Write plan for ``path`` with the given pre- and post-image."""
    return WritePlan(
        tool="Write", file_path=path, existed=pre is not None, pre_image=pre,
        post_image=post, unified_diff="", summary=f"Write {path.name}",
    )


def _apply(snap: Snapshotter, path: Path, post: bytes, *, turn: int, seq: int) -> None:
    """Capture the pre-image the way the registry does, then write."""
    pre = path.read_bytes() if path.exists() else None
    snap.capture(_plan(path, pre, post), turn=turn, seq=seq)
    path.write_bytes(post)


def test_forget_drops_only_that_path(tmp_path: Path) -> None:
    snap = Snapshotter("s", root=tmp_path / "ck")
    a, b = tmp_path / "a.txt", tmp_path / "b.txt"
    a.write_bytes(b"a0")
    _apply(snap, a, b"a1", turn=1, seq=1)
    _apply(snap, b, b"b1", turn=1, seq=2)
    _apply(snap, a, b"a2", turn=1, seq=3)
    assert snap.forget(1, str(a)) == 2
    assert [m["path"] for m in snap.manifests(1)] == [str(b)]
    assert len(list((tmp_path / "ck" / "s" / "turn_0001").glob("*.pre"))) == 0
    assert snap.forget(2, str(a)) == 0
    snap.restore(last_n_turns=1)
    assert a.read_bytes() == b"a2"
    assert not b.exists()


def test_snapshot_files_statuses(tmp_path: Path) -> None:
    snap = Snapshotter("s", root=tmp_path / "ck")
    mod, new, gone = tmp_path / "mod.txt", tmp_path / "new.txt", tmp_path / "gone.txt"
    mod.write_bytes(b"old\n")
    gone.write_bytes(b"bye\n")
    _apply(snap, mod, b"new\n", turn=1, seq=1)
    _apply(snap, new, b"hi\n", turn=1, seq=2)
    _apply(snap, gone, b"x\n", turn=2, seq=1)
    gone.unlink()
    bash_only = tmp_path / "bash.txt"
    files = diffs.snapshot_files(snap, None, [str(bash_only), str(mod)], str(tmp_path))
    by = {f["path"]: f for f in files}
    assert by["mod.txt"]["status"] == "modified"
    assert "-old" in by["mod.txt"]["diff"] and "+new" in by["mod.txt"]["diff"]
    assert by["new.txt"]["status"] == "added"
    assert by["gone.txt"]["status"] == "deleted"
    assert by["bash.txt"] == {"path": "bash.txt", "status": "unsnapshotted", "diff": "", "truncated": False}
    assert [f["path"] for f in diffs.snapshot_files(snap, 2, [], str(tmp_path))] == ["gone.txt"]


def test_revert_from_snapshot(tmp_path: Path) -> None:
    snap = Snapshotter("s", root=tmp_path / "ck")
    mod, new = tmp_path / "mod.txt", tmp_path / "new.txt"
    mod.write_bytes(b"old\n")
    _apply(snap, mod, b"new\n", turn=1, seq=1)
    _apply(snap, new, b"hi\n", turn=1, seq=2)
    assert diffs.revert_from_snapshot(snap, 1, str(mod))
    assert mod.read_bytes() == b"old\n"
    assert diffs.revert_from_snapshot(snap, None, str(new))
    assert not new.exists()
    assert not diffs.revert_from_snapshot(snap, 1, str(tmp_path / "never.txt"))


def test_shadow_ok_refuses_home_and_root(tmp_path: Path) -> None:
    assert diffs.shadow_ok(str(tmp_path))
    assert not diffs.shadow_ok(os.path.expanduser("~"))
    assert not diffs.shadow_ok(os.sep)


def _session(tmp_path: Path, monkeypatch, *, shadow: bool):
    """A Session over ``tmp_path`` whose model writes the file named in each prompt."""
    from coding_harness.core import session as session_mod
    from coding_harness.security import sentinel
    from coding_harness.tools.registry import ToolRegistry
    from coding_harness.tools.write import Write

    def chat(*, messages, **_kw):
        last = messages[-1]
        if last.get("role") != "user" or not str(last["content"]).startswith("write "):
            return {"role": "assistant", "content": "done", "tool_calls": []}
        name = str(last["content"]).split()[1]
        args = {"file_path": str(tmp_path / name), "content": "made\n"}
        call = {"id": "c1", "function": {"name": "Write", "arguments": json.dumps(args)}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session_mod.ollama, "chat", chat)
    monkeypatch.setattr(sentinel, "review", lambda *a, **k: sentinel.SentinelVerdict(True, "ok", "hook"))
    reg = ToolRegistry()
    reg.register(Write())
    reg.confirm_callback = lambda _plan: True
    return session_mod.Session(
        model="fake", registry=reg, system_prompt="s", agentic_review=False,
        verify_repair=False, shadow_checkpoints=shadow,
        snapshotter=Snapshotter("s", root=tmp_path.parent / f"ck-{shadow}"),
    )


def test_session_fallback_diff_and_revert_note(tmp_path: Path, monkeypatch) -> None:
    s = _session(tmp_path, monkeypatch, shadow=False)
    assert s.shadow is None
    s.run_turn("write one.txt")
    s.run_turn("write two.txt")
    assert [f["path"] for f in s.diff(2)["files"]] == ["two.txt"]
    assert sorted(f["path"] for f in s.diff()["files"]) == ["one.txt", "two.txt"]
    assert s.revert_file("two.txt", 2)
    assert not (tmp_path / "two.txt").exists()
    assert s.messages[-1]["content"] == "Operator reverted two.txt to its state before turn 2"
    assert s.messages[-1]["_operator_note"] is True
    s.rewind(1)
    # The note is not a user turn: rewinding one turn cuts back to before turn 2.
    assert [m["content"] for m in s.messages if m["role"] == "user"] == ["write one.txt"]


def test_session_shadow_turn_start_carries_checkpoint(tmp_path: Path, monkeypatch) -> None:
    s = _session(tmp_path, monkeypatch, shadow=True)
    s.run_turn("write one.txt")
    assert s._turn_checkpoints[1] and s._turn_end_checkpoints[1]
    assert [f["status"] for f in s.diff(1)["files"]] == ["added"]


def test_older_turn_diff_reflects_a_later_revert(tmp_path: Path, monkeypatch) -> None:
    s = _session(tmp_path, monkeypatch, shadow=True)
    s.run_turn("write one.txt")
    s.run_turn("write two.txt")
    assert s.revert_file("one.txt", 1)
    assert s.diff(1)["files"] == []
    assert [f["path"] for f in s.diff(2)["files"]] == ["two.txt"]


def test_failed_shadow_restore_keeps_history(tmp_path: Path, monkeypatch) -> None:
    from coding_harness.core.git import RestoreReport

    s = _session(tmp_path, monkeypatch, shadow=True)
    s.run_turn("write one.txt")
    before = list(s.messages)
    assert s.shadow is not None
    monkeypatch.setattr(s.shadow, "restore", lambda sha: RestoreReport([], [], ["boom"]))
    report = s.rewind(1)
    assert report.errors == ["boom"]
    assert s.messages == before
    assert s.last_rewind["failed"] is True and s.last_rewind["user_turn"] == 1
    assert (tmp_path / "one.txt").exists()


def test_operator_note_is_never_elided_as_a_read() -> None:
    from coding_harness.core.context_budget import elide_superseded_reads

    call = {"id": "c1", "function": {"name": "Read", "arguments": json.dumps({"file_path": "/w/a.py"})}}
    note = {"role": "user", "content": "Operator reverted a.py to its state before turn 1", "_operator_note": True}
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": "1\tx"},
        note,
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "name": "Read", "content": "1\ty"},
    ]
    elide_superseded_reads(messages)
    assert messages[2] is note and note["content"].startswith("Operator reverted")
