"""End-to-end Carryall round-trip test (sl-yli.1 acceptance #5).

Spawns the real Carryall MCP server declared in ``.mcp.json``, exercises it
through the harness ``ToolRegistry`` (so Sentinel + audit run), and asserts
that both audit chains record the operation and cross-reference each other.

Skipped by default. Set ``CARRYALL_INTEGRATION=1`` to run the read-side
checks (list vaults). Set ``CARRYALL_INTEGRATION_WRITE=1`` to additionally
exercise envelope compile + write_document with cross-chain verification.
The write path mutates ``~/.carryall/authority.db`` (append-only, hash-chain
preserving); historical entries must never be truncated or modified,
which this test does not do.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import pytest  # type: ignore[import-not-found]

from coding_harness.mcp import MCPClient, load_mcp_config
from coding_harness.security import audit
from coding_harness.tools.registry import ToolRegistry

INTEGRATION = os.environ.get("CARRYALL_INTEGRATION") == "1"
INTEGRATION_WRITE = os.environ.get("CARRYALL_INTEGRATION_WRITE") == "1"

skip_unless_integration = pytest.mark.skipif(
    not INTEGRATION,
    reason="set CARRYALL_INTEGRATION=1 to exercise the live Carryall MCP server",
)
skip_unless_write = pytest.mark.skipif(
    not INTEGRATION_WRITE,
    reason="set CARRYALL_INTEGRATION_WRITE=1 to exercise the write/audit roundtrip",
)


def _carryall_db() -> Path:
    return Path(os.path.expanduser("~/.carryall/authority.db"))


def _make_sentinel_passthrough() -> mock.MagicMock:
    """Return a MagicMock standing in for sentinel.review that always allows.

    We don't want to spawn the real sentinel-gate hook in unit tests — that's
    its own subprocess and its own latency. The dispatch path itself is what
    we're verifying here; sentinel coverage lives in test_sentinel_middleware.
    """
    from coding_harness.security.sentinel import SentinelVerdict

    review = mock.MagicMock(
        return_value=SentinelVerdict(
            allowed=True, reason="test-passthrough", path="hook"
        )
    )
    return review


@skip_unless_integration
class TestCarryallReadRoundTrip(unittest.TestCase):
    """Read-side: list vaults, verify the harness audit chain captures it."""

    def setUp(self) -> None:
        servers = load_mcp_config()
        carryall = next((s for s in servers if s.name == "carryall"), None)
        if carryall is None:
            self.skipTest("no 'carryall' server in .mcp.json")
        self.client = MCPClient(carryall)
        self.client.start()

        self.registry = ToolRegistry(session_id="test-mcp-roundtrip")
        self.registry.register_mcp_server(self.client)

        self.review_patch = mock.patch(
            "coding_harness.tools.registry.sentinel.review",
            _make_sentinel_passthrough(),
        )
        self.review_patch.start()

    def tearDown(self) -> None:
        self.review_patch.stop()
        self.registry.close_mcp_clients()

    def test_carryall_tools_registered(self) -> None:
        names = set(self.registry.tools.keys())
        # Spec contract: 8 Carryall tools per CLAUDE.md.
        expected = {
            "carryall_list_vaults",
            "carryall_get_metadata",
            "carryall_check_access",
            "carryall_query_documents",
            "carryall_audit_log",
            "carryall_read_document",
            "carryall_compile_policy",
            "carryall_write_document",
        }
        missing = expected - names
        self.assertFalse(missing, f"missing Carryall tools: {missing}")

    def test_dispatch_records_audit_entry(self) -> None:
        # Carryall refuses tool calls without an envelope (403). That's fine
        # for this acceptance check — we're verifying that the harness
        # dispatch path delivers the call to Carryall and records the
        # response in our audit chain, not that Carryall performs the read.
        # The compile-then-write path (write roundtrip test) covers the
        # successful execution flow.
        before_seq = self._tail_seq()
        self.registry.dispatch("carryall_list_vaults", {})
        after_seq = self._tail_seq()
        self.assertEqual(after_seq, before_seq + 1)
        last = self._tail_entry()
        self.assertEqual(last["tool"], "carryall_list_vaults")
        # Sentinel allowed it (we patched review to passthrough); the dispatch
        # itself succeeded; Carryall's 403 surfaces in result content but
        # ``allowed`` here means "not blocked by Sentinel."
        self.assertTrue(last["allowed"])

    @staticmethod
    def _tail_entry() -> dict:
        path = audit.AUDIT_PATH
        if not path.exists():
            return {}
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 8192))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.strip().splitlines()):
            if line.strip():
                return json.loads(line)
        return {}

    @classmethod
    def _tail_seq(cls) -> int:
        entry = cls._tail_entry()
        return int(entry["seq"]) if entry else -1


@skip_unless_integration
@skip_unless_write
class TestCarryallWriteRoundTrip(unittest.TestCase):
    """Write-side: compile policy → write document → verify both chains.

    Opt-in via ``CARRYALL_INTEGRATION_WRITE=1``. The write target is a
    timestamped path under ``meta/coding_harness_test/`` so it can be
    located and (optionally) cleaned up without touching audit history.
    """

    def setUp(self) -> None:
        servers = load_mcp_config()
        carryall = next((s for s in servers if s.name == "carryall"), None)
        if carryall is None:
            self.skipTest("no 'carryall' server in .mcp.json")
        self.client = MCPClient(carryall)
        self.client.start()

        self.registry = ToolRegistry(session_id="test-mcp-write-roundtrip")
        self.registry.register_mcp_server(self.client)
        self.review_patch = mock.patch(
            "coding_harness.tools.registry.sentinel.review",
            _make_sentinel_passthrough(),
        )
        self.review_patch.start()

    def tearDown(self) -> None:
        self.review_patch.stop()
        self.registry.close_mcp_clients()

    def test_compile_and_write_cross_references_audit(self) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        rel_path = f"coding_harness_test/{ts}-roundtrip.md"

        # 1) Compile a write envelope for meta vault.
        compile_result = self.registry.dispatch(
            "carryall_compile_policy",
            {
                "intent": (
                    f"write a test document to meta vault at {rel_path}"
                ),
                "agent": "executive-agent",
            },
        )
        self.assertFalse(
            compile_result.is_error,
            f"compile_policy failed: {compile_result.content}",
        )
        envelope = _extract_envelope(compile_result.content)
        self.assertIsNotNone(
            envelope, f"no envelope in compile result: {compile_result.content}"
        )

        # 2) Write the document.
        write_result = self.registry.dispatch(
            "carryall_write_document",
            {
                "envelope": envelope,
                "vault": "meta",
                "path": rel_path,
                "content": (
                    f"# coding_harness MCP roundtrip test\n\n"
                    f"Written at {ts} by sl-yli.1 acceptance test.\n"
                ),
            },
        )
        self.assertFalse(
            write_result.is_error,
            f"write_document failed: {write_result.content}",
        )

        # 3) The harness audit tail entry must reference a Carryall audit id.
        last = TestCarryallReadRoundTrip._tail_entry()
        self.assertEqual(last["tool"], "carryall_write_document")
        carryall_audit_id = last.get("carryall_audit_id")
        self.assertIsNotNone(
            carryall_audit_id,
            f"harness audit entry missing carryall_audit_id: {last}",
        )

        # 4) The Carryall authority.db must contain a matching audit row.
        db = _carryall_db()
        self.assertTrue(db.exists(), f"{db} not present")
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT count(*) FROM audit_trail WHERE id = ?",
                (str(carryall_audit_id),),
            ).fetchone()
        self.assertEqual(
            row[0], 1,
            f"Carryall audit row {carryall_audit_id} not found in authority.db",
        )

        # 5) Confirm the harness chain still verifies after the round-trip.
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, f"harness audit chain broken: {msg}")

        # 6) Confirm Carryall's chain still verifies. We invoke its CLI
        #    rather than re-implementing chain verification; audit tables
        #    are never touched directly.
        proc = subprocess.run(
            [sys.executable,
             "-m", "authority_runtime.cli", "audit", "verify"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(
            proc.returncode, 0,
            f"carryall audit verify failed: {proc.stderr}",
        )


def _extract_envelope(text: str) -> dict | None:
    """Pull a JSON envelope out of an MCP text response.

    Carryall's compile_policy returns the envelope as the body of a text
    content block. We try whole-blob parse first, then a simple brace
    scan to handle the case where Carryall prepends a status sentence.
    """
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            if "envelope" in parsed and isinstance(parsed["envelope"], dict):
                return parsed["envelope"]
            return parsed
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        try:
            parsed = json.loads(text[start:])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        start = text.find("{", start + 1)
    return None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
