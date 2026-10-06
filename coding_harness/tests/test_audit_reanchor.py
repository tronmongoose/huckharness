"""Acknowledged chain breaks: a fork can be recorded without rewriting history.

A tamper-evident log that is edited to make it verify is no longer evidence. So
a break is never repaired in place — it is acknowledged out of band, pinned to
the exact hashes and to a digest of every byte before it. Altering any earlier
line invalidates the acknowledgement and the chain goes red again.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.security import audit, audit_breaks


class ReanchorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.meta = Path(self.tmp.name)
        self.audit_path = self.meta / "audit.jsonl"
        self.breaks_path = self.meta / "audit-breaks.jsonl"
        self._patches = [
            patch.object(audit, "META_DIR", self.meta),
            patch.object(audit, "AUDIT_PATH", self.audit_path),
            patch.object(audit, "ANCHORS_PATH", self.meta / "anchors.jsonl"),
            patch.object(audit, "LOCK_PATH", self.meta / "audit.lock"),
            patch.object(audit_breaks, "BREAKS_PATH", self.breaks_path),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _append(self, n: int, session: str = "s") -> None:
        for i in range(n):
            audit.append(
                session_id=session, tool="Read", args={"i": i}, result=f"r{i}",
                allowed=True, sentinel_reason="approved", sentinel_path="fast",
            )

    def _fork_at(self, line_index: int) -> None:
        """Duplicate one entry's (prev_hash, seq) the way two racing writers did.

        The orphan is internally valid — its own hash recomputes — so the walk
        accepts it and then trips on the *next* line, which is the shape of the
        real 2026-08-22 fork.
        """
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        orphan = json.loads(lines[line_index])
        orphan.pop("sig", None)
        orphan.pop("signer", None)
        orphan["session_id"] = "racer"
        payload = {k: v for k, v in orphan.items() if k != "hash"}
        orphan["hash"] = audit._hash_entry(orphan["prev_hash"], payload)
        lines.insert(line_index, json.dumps(orphan, ensure_ascii=False))
        self.audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def _ack_first_break(self, reason: str = "test fork") -> dict:
        result = audit.scan_chain()
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.break_info)
        return audit_breaks.record(result.break_info, reason=reason, acknowledged_by="test")

    def test_unacknowledged_fork_fails(self) -> None:
        self._append(5)
        self._fork_at(3)
        ok, msg = audit.verify_chain()
        self.assertFalse(ok)
        self.assertIn("line 5", msg)

    def test_acknowledged_fork_verifies(self) -> None:
        self._append(5)
        self._fork_at(3)
        self._ack_first_break()
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        self.assertIn("1 acknowledged break", msg)

    def test_acknowledgement_does_not_launder_earlier_tampering(self) -> None:
        self._append(5)
        self._fork_at(3)
        self._ack_first_break()
        self.assertTrue(audit.verify_chain()[0])

        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["result_digest"] = "0" * 16
        lines[1] = json.dumps(entry, ensure_ascii=False)
        self.audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        ok, msg = audit.verify_chain()
        self.assertFalse(ok, "editing a line before the break must re-break the chain")

    def test_acknowledgement_is_pinned_to_its_line(self) -> None:
        self._append(5)
        self._fork_at(3)
        ack = self._ack_first_break()
        moved = dict(ack, line=ack["line"] + 1)
        self.breaks_path.write_text(json.dumps(moved) + "\n", encoding="utf-8")
        ok, _msg = audit.verify_chain()
        self.assertFalse(ok, "an ack for a different line must not apply")

    def test_second_unacknowledged_break_still_fails(self) -> None:
        self._append(8)
        self._fork_at(6)
        self._fork_at(2)
        self._ack_first_break()
        ok, _msg = audit.verify_chain()
        self.assertFalse(ok, "acknowledging one break must not excuse the next")

    def test_cli_refuses_to_acknowledge_a_line_with_no_break(self) -> None:
        self._append(5)
        self._fork_at(3)
        self.assertEqual(audit.main(["--acknowledge-break", "--line", "2", "--reason", "x"]), 2)
        self.assertFalse(self.breaks_path.exists())

    def test_cli_acknowledges_the_real_break(self) -> None:
        self._append(5)
        self._fork_at(3)
        rc = audit.main(["--acknowledge-break", "--line", "5", "--reason", "concurrent fork"])
        self.assertEqual(rc, 0)
        self.assertEqual(audit.main([]), 0)

    def test_cli_refuses_when_chain_is_intact(self) -> None:
        self._append(4)
        self.assertEqual(audit.main(["--acknowledge-break", "--line", "3", "--reason", "x"]), 2)


if __name__ == "__main__":
    unittest.main()
