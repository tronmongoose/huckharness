"""Unit tests for the JIT permission broker (P1).

The broker blocks a turn thread in ``request()`` until another thread
``resolve()``s it — the cross-thread handoff that lets an HTTP operator grant
an out-of-envelope call mid-turn. These tests drive both sides with real
threads (no server) and cover the fail-closed timeout, allow_once, and the
allow_always path that widens the envelope through ``on_grant``.
"""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core.envelope import SessionEnvelope, grant_for
from coding_harness.core.permissions import (
    DEFAULT_TIMEOUT_S,
    BrokerDecision,
    PermissionBroker,
)
from coding_harness.security import audit
from coding_harness.tools.base import Tool, ToolResult
from coding_harness.tools.registry import ToolRegistry


def _wait_for_pending(broker: PermissionBroker, timeout: float = 2.0) -> str:
    """Spin until one request is pending; return its req_id."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = broker.list_pending()
        if pending:
            return pending[0]["req_id"]
        time.sleep(0.01)
    raise AssertionError("no pending request appeared")


class TestPermissionBroker(unittest.TestCase):
    def test_timeout_denies_fail_closed(self) -> None:
        broker = PermissionBroker(emit=lambda *_: None)
        decision = broker.request("Write", {"file_path": "/x"}, "why", timeout=0.1)
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.decision, "timeout")
        # Timed-out request is no longer pending.
        self.assertEqual(broker.list_pending(), [])

    def test_allow_once_unblocks_and_allows(self) -> None:
        broker = PermissionBroker(emit=lambda *_: None)
        holder: dict[str, object] = {}

        def _run() -> None:
            holder["decision"] = broker.request(
                "Write", {"file_path": "/x"}, "why", timeout=2.0
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        req_id = _wait_for_pending(broker)
        self.assertTrue(broker.resolve(req_id, "allow_once"))
        t.join(timeout=2.0)
        decision = holder["decision"]
        self.assertTrue(decision.allowed)  # type: ignore[union-attr]
        self.assertEqual(decision.decision, "allow_once")  # type: ignore[union-attr]

    def test_deny_resolution_blocks_call(self) -> None:
        broker = PermissionBroker(emit=lambda *_: None)
        holder: dict[str, object] = {}

        def _run() -> None:
            holder["decision"] = broker.request(
                "Bash", {"command": "rm -rf /"}, "why", timeout=2.0
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        req_id = _wait_for_pending(broker)
        self.assertTrue(broker.resolve(req_id, "deny"))
        t.join(timeout=2.0)
        self.assertFalse(holder["decision"].allowed)  # type: ignore[union-attr]

    def test_allow_always_calls_on_grant_and_widens_envelope(self) -> None:
        env = SessionEnvelope()

        def _on_grant(req) -> None:
            env.add_grant(grant_for(req.tool, req.args, expiry_minutes=req.expiry_minutes))

        broker = PermissionBroker(emit=lambda *_: None, on_grant=_on_grant)
        holder: dict[str, object] = {}

        def _run() -> None:
            holder["decision"] = broker.request(
                "Read", {"file_path": "/repo/a.py"}, "why", timeout=2.0
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        req_id = _wait_for_pending(broker)
        self.assertTrue(broker.resolve(req_id, "allow_always", expiry_minutes=30))
        t.join(timeout=2.0)
        self.assertTrue(holder["decision"].allowed)  # type: ignore[union-attr]
        # The envelope now allows the previously-denied call.
        self.assertTrue(env.check("Read", {"file_path": "/repo/a.py"}).allowed)

    def test_resolve_unknown_returns_false(self) -> None:
        broker = PermissionBroker(emit=lambda *_: None)
        self.assertFalse(broker.resolve("does-not-exist", "allow_once"))

    def test_double_resolve_second_returns_false(self) -> None:
        # First decision is final; a second resolve on the same id can't flip it.
        broker = PermissionBroker(emit=lambda *_: None)
        holder: dict[str, object] = {}

        def _run() -> None:
            holder["decision"] = broker.request(
                "Write", {"file_path": "/x"}, "why", timeout=2.0
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        req_id = _wait_for_pending(broker)
        self.assertTrue(broker.resolve(req_id, "deny"))
        self.assertFalse(broker.resolve(req_id, "allow_always"))
        t.join(timeout=2.0)
        self.assertFalse(holder["decision"].allowed)  # type: ignore[union-attr]

    def test_deny_all_unblocks_parked_request(self) -> None:
        # interrupt/revoke path: a turn parked in request() must unblock now,
        # not wait out the timeout.
        broker = PermissionBroker(emit=lambda *_: None)
        holder: dict[str, object] = {}

        def _run() -> None:
            holder["decision"] = broker.request(
                "Bash", {"command": "x"}, "why", timeout=30.0
            )

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        _wait_for_pending(broker)
        denied = broker.deny_all(reason="revoked")
        self.assertEqual(denied, 1)
        t.join(timeout=2.0)
        self.assertFalse(t.is_alive())
        self.assertFalse(holder["decision"].allowed)  # type: ignore[union-attr]
        self.assertEqual(broker.list_pending(), [])

    def test_emit_fires_permission_request(self) -> None:
        events: list[tuple[str, dict]] = []
        broker = PermissionBroker(emit=lambda kind, payload: events.append((kind, payload)))

        def _run() -> None:
            broker.request("Write", {"file_path": "/x"}, "why", timeout=1.0)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        req_id = _wait_for_pending(broker)
        broker.resolve(req_id, "deny")
        t.join(timeout=2.0)
        kinds = [k for k, _ in events]
        self.assertIn("permission_request", kinds)
        self.assertIn("permission_resolved", kinds)


class _ReadSpy(Tool):
    name = "Read"
    description = "spy"
    parameters = {"type": "object", "properties": {}}

    def run(self, args):  # type: ignore[no-untyped-def]
        return ToolResult(content="ran")


class TestRegistryBrokerTimeout(unittest.TestCase):
    """The registry caps a brokered wait by ``deadline_at`` when one is set."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _registry(self) -> tuple[ToolRegistry, mock.MagicMock]:
        broker = mock.MagicMock()
        broker.request.return_value = BrokerDecision(False, "deny")
        reg = ToolRegistry(
            session_id="dl", envelope=SessionEnvelope(grants=[]), permission_broker=broker,
        )
        reg.register(_ReadSpy())
        return reg, broker

    def test_unset_deadline_keeps_default_timeout(self) -> None:
        reg, broker = self._registry()
        self.assertEqual(reg.deadline_remaining_s(), float("inf"))
        result = reg.dispatch("Read", {"file_path": "/x"})
        self.assertTrue(result.is_error)
        self.assertEqual(broker.request.call_args.kwargs["timeout"], DEFAULT_TIMEOUT_S)

    def test_deadline_caps_broker_wait(self) -> None:
        reg, broker = self._registry()
        reg.deadline_at = time.time() + 10.0
        reg.dispatch("Read", {"file_path": "/x"})
        timeout = broker.request.call_args.kwargs["timeout"]
        self.assertLessEqual(timeout, 10.0)
        self.assertGreater(timeout, 8.0)

    def test_passed_deadline_clamps_to_zero(self) -> None:
        reg, broker = self._registry()
        reg.deadline_at = time.time() - 5.0
        self.assertEqual(reg.deadline_remaining_s(), 0.0)
        reg.dispatch("Read", {"file_path": "/x"})
        self.assertEqual(broker.request.call_args.kwargs["timeout"], 0.0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
