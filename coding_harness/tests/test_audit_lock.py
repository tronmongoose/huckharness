"""Cross-process serialization of the audit chain.

Nine append functions each do read-head-then-write. Without a lock spanning
that pair, two processes read the same head and write the same seq — which is
exactly what happened on 2026-08-22 at seq 1300 and forked the live chain.
These tests run real subprocesses; threads would not prove the cross-process
claim.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD = textwrap.dedent(
    """
    import sys
    from coding_harness.security import audit
    session, count = sys.argv[1], int(sys.argv[2])
    for i in range(count):
        audit.append(
            session_id=session, tool="Bash", args={"i": i}, result="r",
            allowed=True, sentinel_reason="ok", sentinel_path="fast",
        )
    """
)


class AuditLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.meta = Path(self.tmp.name)
        self.child = self.meta / "child.py"
        self.child.write_text(_CHILD, encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["HARNESS_META_DIR"] = str(self.meta)
        env["PYTHONPATH"] = str(REPO_ROOT)
        return env

    def test_concurrent_processes_produce_one_valid_chain(self) -> None:
        procs = [
            subprocess.Popen(
                [sys.executable, str(self.child), f"sess-{n}", "8"],
                env=self._env(), cwd=str(REPO_ROOT),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            for n in range(4)
        ]
        for p in procs:
            _out, err = p.communicate(timeout=120)
            self.assertEqual(p.returncode, 0, err.decode("utf-8", "replace"))

        audit_path = self.meta / "audit.jsonl"
        lines = audit_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 32)

        seqs = [json.loads(ln)["seq"] for ln in lines]
        self.assertEqual(seqs, list(range(32)), "seq must be gapless and unique")

        # Verify in a fresh process so module constants pick up the temp meta dir.
        check = subprocess.run(
            [sys.executable, "-m", "coding_harness.security.audit"],
            env=self._env(), cwd=str(REPO_ROOT), capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_lock_is_exclusive_and_bounded(self) -> None:
        from coding_harness.security import audit

        with mock.patch.object(audit, "META_DIR", self.meta), \
             mock.patch.object(audit, "LOCK_PATH", self.meta / "audit.lock"):
            held = threading.Event()
            release = threading.Event()

            def _holder() -> None:
                with audit._chain_lock():
                    held.set()
                    release.wait(timeout=10)

            t = threading.Thread(target=_holder)
            t.start()
            try:
                self.assertTrue(held.wait(timeout=10))
                started = time.monotonic()
                with self.assertRaises(audit.ChainLockTimeout):
                    with audit._chain_lock(timeout_s=0.3):
                        pass
                self.assertLess(time.monotonic() - started, 5.0)
            finally:
                release.set()
                t.join(timeout=10)


if __name__ == "__main__":
    unittest.main()
