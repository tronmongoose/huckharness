"""Tests for per-turn snapshots and Session.rewind().

Snapshots are written under a session-scoped checkpoint dir during ``apply()``
of a write-class tool. ``Session.rewind(n)`` restores pre-images for the last
``n`` user-prompt turns and truncates the conversation history accordingly.

These tests exercise the Snapshotter directly (cheap, deterministic) and the
Session.rewind() path through a fake model that emits scripted tool calls,
which is the same shape the existing ``test_repl_mode.py`` already uses.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.core.session import Session
from coding_harness.security import audit, sentinel
from coding_harness.security.snapshot import Snapshotter
from coding_harness.tools.base import WritePlan
from coding_harness.tools.edit import Edit
from coding_harness.tools.registry import ToolRegistry
from coding_harness.tools.write import Write


def _allow_all():
    return patch.object(
        sentinel, "review",
        return_value=sentinel.SentinelVerdict(allowed=True, reason="ok", path="hook"),
    )


class SnapshotterUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_capture_writes_pre_image_and_manifest(self) -> None:
        snap = Snapshotter("sess-1", root=self.tmp_path)
        target = self.tmp_path / "f.txt"
        target.write_text("v1")

        plan = WritePlan(
            tool="Edit",
            file_path=target,
            existed=True,
            pre_image=b"v1",
            post_image=b"v2",
            unified_diff="--- a\n+++ b\n@@\n-v1\n+v2",
            summary="Edit f.txt",
        )
        ref = snap.capture(plan, turn=1, seq=1)

        self.assertTrue(ref.manifest_path.exists())
        self.assertTrue(ref.pre_image_path.exists())
        self.assertEqual(ref.pre_image_path.read_bytes(), b"v1")
        manifest = json.loads(ref.manifest_path.read_text())
        self.assertEqual(manifest["existed"], True)
        self.assertEqual(manifest["path"], str(target))

    def test_capture_omits_pre_image_for_new_file(self) -> None:
        snap = Snapshotter("sess-1", root=self.tmp_path)
        target = self.tmp_path / "new.txt"
        plan = WritePlan(
            tool="Write",
            file_path=target,
            existed=False,
            pre_image=None,
            post_image=b"hi",
            unified_diff="--- /dev/null\n+++ new.txt\n+hi",
            summary="Write new.txt",
        )
        ref = snap.capture(plan, turn=1, seq=1)

        self.assertIsNone(ref.pre_image_path)
        manifest = json.loads(ref.manifest_path.read_text())
        self.assertEqual(manifest["existed"], False)
        self.assertIsNone(manifest["pre_image_path"])

    def test_restore_brings_back_pre_image(self) -> None:
        snap = Snapshotter("sess-1", root=self.tmp_path)
        target = self.tmp_path / "f.txt"
        target.write_text("v1")
        snap.capture(
            WritePlan(
                tool="Edit", file_path=target, existed=True,
                pre_image=b"v1", post_image=b"v2",
                unified_diff="d", summary="Edit f.txt",
            ),
            turn=1, seq=1,
        )
        target.write_text("v2")  # simulate the apply

        report = snap.restore(last_n_turns=1)
        self.assertEqual(report.turns_rewound, 1)
        self.assertEqual(report.files_restored, 1)
        self.assertEqual(target.read_text(), "v1")

    def test_restore_deletes_files_that_did_not_exist(self) -> None:
        snap = Snapshotter("sess-1", root=self.tmp_path)
        target = self.tmp_path / "fresh.txt"
        snap.capture(
            WritePlan(
                tool="Write", file_path=target, existed=False,
                pre_image=None, post_image=b"hi",
                unified_diff="d", summary="Write fresh.txt",
            ),
            turn=1, seq=1,
        )
        target.write_text("hi")  # simulate apply

        report = snap.restore(last_n_turns=1)
        self.assertEqual(report.files_deleted, 1)
        self.assertFalse(target.exists())

    def test_restore_is_idempotent_after_consumption(self) -> None:
        snap = Snapshotter("sess-1", root=self.tmp_path)
        target = self.tmp_path / "f.txt"
        target.write_text("v1")
        snap.capture(
            WritePlan(
                tool="Edit", file_path=target, existed=True,
                pre_image=b"v1", post_image=b"v2",
                unified_diff="d", summary="Edit",
            ),
            turn=1, seq=1,
        )
        target.write_text("v2")
        snap.restore(last_n_turns=1)
        # Second restore: nothing left to do.
        report = snap.restore(last_n_turns=1)
        self.assertEqual(report.turns_rewound, 0)
        self.assertEqual(report.files_restored, 0)


# ── Session-level integration ──────────────────────────────────────


class _FakeOllama:
    """Drives Session.run_turn with a scripted sequence of model messages.

    Mirrors the pattern used by tests/test_repl_mode.py.
    """

    def __init__(self, scripts: list[list[dict]]):
        # scripts[i] is the list of messages (one per agent turn) for the i-th
        # user prompt. The last message in each sublist must have no tool_calls
        # so the loop terminates.
        self.scripts = scripts
        self.idx = 0
        self.subidx = 0

    def chat(self, *, model, messages, tools, **_kw):
        msg = self.scripts[self.idx][self.subidx]
        self.subidx += 1
        if self.subidx >= len(self.scripts[self.idx]):
            self.idx += 1
            self.subidx = 0
        return msg


def _tool_call(tool: str, args: dict, *, call_id: str = "c1") -> dict:
    return {
        "id": call_id,
        "function": {"name": tool, "arguments": json.dumps(args)},
    }


class SessionRewindTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patches = [
            patch.object(audit, "META_DIR", self.tmp_path),
            patch.object(audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"),
            patch.object(audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"),
            # Sessions write their own log under SESSIONS_DIR — point that at tmp too.
            patch(
                "coding_harness.core.session.SESSIONS_DIR",
                self.tmp_path / "sessions",
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _build_session(self, fake: _FakeOllama) -> Session:
        reg = ToolRegistry()
        reg.register(Write())
        reg.register(Edit())
        # Auto-accept (we're testing rewind, not the diff prompt itself).
        reg.confirm_callback = lambda _plan: True
        snapshotter = Snapshotter("sess-rewind", root=self.tmp_path / "checkpoints")
        session = Session(
            model="fake-model",
            registry=reg,
            system_prompt="be terse",
            snapshotter=snapshotter,
            agentic_review=False,
        )
        # Patch in the fake model for this session only.
        self._chat_patch = patch(
            "coding_harness.core.session.ollama.chat",
            side_effect=fake.chat,
        )
        self._chat_patch.start()
        self.addCleanup(self._chat_patch.stop)
        return session

    def test_rewind_restores_overwritten_file(self) -> None:
        target = self.tmp_path / "code.py"
        target.write_text("v1\n")

        fake = _FakeOllama([
            [
                {"role": "assistant", "content": "", "tool_calls": [
                    _tool_call("Edit", {
                        "file_path": str(target),
                        "old_string": "v1",
                        "new_string": "v2",
                    }),
                ]},
                {"role": "assistant", "content": "done", "tool_calls": []},
            ],
        ])
        session = self._build_session(fake)

        with _allow_all():
            session.run_turn("change v1 to v2")

        self.assertEqual(target.read_text(), "v2\n")

        with _allow_all():
            report = session.rewind(1)

        self.assertEqual(report.turns_rewound, 1)
        self.assertEqual(report.files_restored, 1)
        self.assertEqual(target.read_text(), "v1\n")

        # Conversation truncated back before the user message.
        roles = [m.get("role") for m in session.messages]
        self.assertEqual(roles, ["system"])

    def test_rewind_deletes_newly_created_file(self) -> None:
        target = self.tmp_path / "fresh.txt"
        self.assertFalse(target.exists())

        fake = _FakeOllama([
            [
                {"role": "assistant", "content": "", "tool_calls": [
                    _tool_call("Write", {
                        "file_path": str(target),
                        "content": "hello\n",
                    }),
                ]},
                {"role": "assistant", "content": "done", "tool_calls": []},
            ],
        ])
        session = self._build_session(fake)

        with _allow_all():
            session.run_turn("create fresh.txt")
        self.assertEqual(target.read_text(), "hello\n")

        with _allow_all():
            report = session.rewind(1)

        self.assertEqual(report.files_deleted, 1)
        self.assertFalse(target.exists())

    def test_rewind_two_turns_restores_oldest_state(self) -> None:
        target = self.tmp_path / "code.py"
        target.write_text("v0\n")

        # Two user prompts, each does one Edit.
        fake = _FakeOllama([
            [
                {"role": "assistant", "content": "", "tool_calls": [
                    _tool_call("Edit", {
                        "file_path": str(target),
                        "old_string": "v0",
                        "new_string": "v1",
                    }),
                ]},
                {"role": "assistant", "content": "ok", "tool_calls": []},
            ],
            [
                {"role": "assistant", "content": "", "tool_calls": [
                    _tool_call("Edit", {
                        "file_path": str(target),
                        "old_string": "v1",
                        "new_string": "v2",
                    }),
                ]},
                {"role": "assistant", "content": "ok", "tool_calls": []},
            ],
        ])
        session = self._build_session(fake)

        with _allow_all():
            session.run_turn("v0 → v1")
            session.run_turn("v1 → v2")

        self.assertEqual(target.read_text(), "v2\n")

        with _allow_all():
            report = session.rewind(2)

        self.assertEqual(report.turns_rewound, 2)
        # Two turns = two restore steps; both targeted the same path, so
        # files_restored counts both writes — but the *content* lands at v0.
        self.assertEqual(target.read_text(), "v0\n")
        roles = [m.get("role") for m in session.messages]
        self.assertEqual(roles, ["system"])


if __name__ == "__main__":
    unittest.main()
