"""Per-bead outcome receipts in the audit chain."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.security import audit


class ReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tp = Path(self.tmp.name)
        self._p = [
            patch.object(audit, "META_DIR", self.tp),
            patch.object(audit, "AUDIT_PATH", self.tp / "audit.jsonl"),
            patch.object(audit, "ANCHORS_PATH", self.tp / "anchors.jsonl"),
        ]
        for p in self._p:
            p.start()

    def tearDown(self) -> None:
        for p in self._p:
            p.stop()
        self.tmp.cleanup()

    def _rows(self) -> list[dict]:
        path = self.tp / "audit.jsonl"
        return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]

    def test_receipt_fields(self) -> None:
        audit.append_receipt(
            session_id="s1", bead_id="sl-x", bead_class="task", tokens=1200,
            inner_steps=None, verify_passed=True, diff_files=3, diff_lines=42,
            pr_url="https://gh/pr/1", exit_code=0,
        )
        row = self._rows()[-1]
        self.assertEqual(row["kind"], "receipt")
        self.assertEqual(row["bead_id"], "sl-x")
        self.assertEqual(row["bead_class"], "task")
        self.assertTrue(row["verify_passed"])
        self.assertEqual(row["pr_url"], "https://gh/pr/1")
        self.assertIn("hash", row)

    def test_reconcile_fields(self) -> None:
        audit.append_receipt_reconcile(
            bead_id="sl-x", pr_url="https://gh/pr/1", merged=True,
            merged_at="2026-08-02T00:00:00Z",
        )
        row = self._rows()[-1]
        self.assertEqual(row["kind"], "receipt_reconcile")
        self.assertTrue(row["merged"])

    def test_chain_stays_valid_with_receipts(self) -> None:
        audit.append(
            session_id="s", tool="Read", args={"file_path": "/x"}, result="c",
            allowed=True, sentinel_reason="ok", sentinel_path="hook",
        )
        audit.append_receipt(
            session_id="s", bead_id="sl-x", bead_class="task", tokens=1,
            inner_steps=None, verify_passed=False, diff_files=0, diff_lines=0,
            pr_url=None, exit_code=1,
        )
        audit.append_receipt_reconcile(
            bead_id="sl-x", pr_url=None, merged=False, merged_at=None,
        )
        ok, detail = audit.verify_chain(self.tp / "audit.jsonl")
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
