"""Hash-chained audit log: integrity + tampering detection."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.security import audit


class AuditChainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patches = [
            patch.object(audit, "META_DIR", self.tmp_path),
            patch.object(audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"),
            patch.object(audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _append(self, n: int) -> None:
        for i in range(n):
            audit.append(
                session_id="test",
                tool="Read",
                args={"file_path": f"/tmp/x{i}"},
                result=f"contents of x{i}",
                allowed=True,
                sentinel_reason="approved",
                sentinel_path="hook",
            )

    def test_chain_validates_after_appends(self) -> None:
        self._append(5)
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        self.assertIn("5 entries", msg)

    def test_seq_increments_and_anchors_written(self) -> None:
        self._append(3)
        lines = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertEqual([json.loads(line)["seq"] for line in lines], [0, 1, 2])
        anchors = (self.tmp_path / "anchors.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(anchors), 3)
        # Last anchor hash matches last audit entry hash.
        self.assertEqual(
            json.loads(anchors[-1])["hash"],
            json.loads(lines[-1])["hash"],
        )

    def test_tampering_breaks_chain(self) -> None:
        self._append(4)
        path = self.tmp_path / "audit.jsonl"
        lines = path.read_text().splitlines()
        # Mutate the args_digest of entry 1 (not the hash field — we want to
        # prove that changing the *content* of an entry invalidates the chain).
        entry = json.loads(lines[1])
        entry["args_digest"] = "deadbeefdeadbeef"
        lines[1] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n")

        ok, msg = audit.verify_chain()
        self.assertFalse(ok)
        self.assertIn("hash mismatch", msg)

    def test_dropping_entry_breaks_chain(self) -> None:
        self._append(4)
        path = self.tmp_path / "audit.jsonl"
        lines = path.read_text().splitlines()
        del lines[2]
        path.write_text("\n".join(lines) + "\n")

        ok, msg = audit.verify_chain()
        self.assertFalse(ok)
        # Could be either seq or prev_hash mismatch depending on which check
        # fires first; both indicate the chain is broken.
        self.assertTrue("seq mismatch" in msg or "prev_hash mismatch" in msg, msg)

    def test_turn_entry_carries_telemetry_fields_and_chains(self) -> None:
        """sl-nmc.4: append_turn writes the five new fields and stays in chain."""
        # Mix tool-call entries with a turn entry to prove the chain holds
        # across both kinds — order matters for hashing.
        self._append(2)
        audit.append_turn(
            session_id="test",
            user_turn=1,
            model="mistral-small3.2:latest",
            route_reason="complexity_low",
            tokens_in=512,
            tokens_out=128,
            thinking_tokens=None,
            tool_call_count=2,
            inner_steps=3,
            halted_reason="model_done",
        )
        self._append(1)

        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)

        lines = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        self.assertEqual(len(lines), 4)
        turn = json.loads(lines[2])
        self.assertEqual(turn["kind"], "turn")
        self.assertEqual(turn["model"], "mistral-small3.2:latest")
        self.assertEqual(turn["route_reason"], "complexity_low")
        self.assertEqual(turn["tokens_in"], 512)
        self.assertEqual(turn["tokens_out"], 128)
        self.assertIsNone(turn["thinking_tokens"])
        self.assertEqual(turn["tool_call_count"], 2)
        self.assertEqual(turn["inner_steps"], 3)
        self.assertEqual(turn["halted_reason"], "model_done")
        # Seq is monotonic across both kinds.
        self.assertEqual(
            [json.loads(line)["seq"] for line in lines], [0, 1, 2, 3]
        )

    def test_tampering_with_turn_entry_breaks_chain(self) -> None:
        audit.append_turn(
            session_id="test",
            user_turn=1,
            model="m",
            route_reason="complexity_low",
            tokens_in=10,
            tokens_out=5,
            thinking_tokens=None,
            tool_call_count=0,
            inner_steps=1,
            halted_reason="model_done",
        )
        self._append(1)
        path = self.tmp_path / "audit.jsonl"
        lines = path.read_text().splitlines()
        entry = json.loads(lines[0])
        entry["tokens_in"] = 999  # tamper
        lines[0] = json.dumps(entry)
        path.write_text("\n".join(lines) + "\n")

        ok, msg = audit.verify_chain()
        self.assertFalse(ok)
        self.assertIn("hash mismatch", msg)

    def test_blocked_call_is_audited_too(self) -> None:
        audit.append(
            session_id="test",
            tool="Bash",
            args={"command": "rm -rf /"},
            result="BLOCKED",
            allowed=False,
            sentinel_reason="destructive_command",
            sentinel_path="fast",
        )
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        entries = [json.loads(line) for line in
                   (self.tmp_path / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0]["allowed"])
        self.assertEqual(entries[0]["sentinel_reason"], "destructive_command")


if __name__ == "__main__":
    unittest.main()
