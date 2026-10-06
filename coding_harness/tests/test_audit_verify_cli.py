"""The audit-verify entry point: exit 0 on an intact chain, 1 on a broken one.

verify_chain existed for months with no caller outside these tests, which is the
failure mode to avoid: a tamper-evident log nobody verifies is decoration.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.security import audit


class AuditVerifyCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.audit_path = self.tmp_path / "audit.jsonl"
        self._patches = [
            patch.object(audit, "META_DIR", self.tmp_path),
            patch.object(audit, "AUDIT_PATH", self.audit_path),
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

    def test_intact_chain_exits_zero(self) -> None:
        self._append(3)
        self.assertEqual(audit.main([]), 0)

    def test_missing_audit_file_exits_zero(self) -> None:
        self.assertFalse(self.audit_path.exists())
        self.assertEqual(audit.main([]), 0)

    def test_tampered_entry_exits_nonzero(self) -> None:
        self._append(3)
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[1])
        entry["result"] = "tampered after the fact"
        lines[1] = json.dumps(entry, ensure_ascii=False)
        self.audit_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertEqual(audit.main([]), 1)

    def test_truncated_chain_exits_nonzero(self) -> None:
        self._append(3)
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        # Drop the middle entry: seq and prev_hash both break.
        self.audit_path.write_text(lines[0] + "\n" + lines[2] + "\n", encoding="utf-8")
        self.assertEqual(audit.main([]), 1)

    def test_path_flag_targets_another_file(self) -> None:
        other = self.tmp_path / "elsewhere.jsonl"
        other.write_text('{"not": "a chain"}\n', encoding="utf-8")
        self.assertEqual(audit.main(["--path", str(other)]), 1)
        self.assertEqual(audit.main(["--path", str(self.tmp_path / "absent.jsonl")]), 0)


if __name__ == "__main__":
    unittest.main()
