"""Tests for Ed25519 per-action audit signing (P3).

Uses a real PyNaCl keypair in a monkeypatched keystore so signing and
verification round-trip without touching the operator's real config. Verifies that
unsigned rows (the entire pre-P3 chain) still pass.
"""
from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.security import audit


class _FakeKeyStore:
    """Minimal AgentKeyStore stand-in backed by one in-memory nacl key."""

    def __init__(self, name: str, signing_key) -> None:
        self._name = name
        self._key = signing_key

    def has_key(self, name: str) -> bool:
        return name == self._name

    def load_signing_key(self, name: str):
        if name != self._name:
            raise KeyError(name)
        return self._key


class TestAuditSigning(unittest.TestCase):
    def setUp(self) -> None:
        from nacl.signing import SigningKey
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self.name = "startup-agent"
        self.sk = SigningKey.generate()
        pub_b64 = base64.b64encode(self.sk.verify_key.encode()).decode()

        # Reset module-level caches so each test resolves fresh.
        audit._keystore = _FakeKeyStore(self.name, self.sk)
        audit._keystore_tried = True
        audit._signer_cache = {}
        audit._pubkeys = {self.name: pub_b64}

        self.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        audit._keystore = None
        audit._keystore_tried = False
        audit._signer_cache = {}
        audit._pubkeys = None
        self.tmp.cleanup()

    def _append(self, identity):
        return audit.append(
            session_id="s1", tool="Read", args={"file_path": "/x"},
            result="data", allowed=True, sentinel_reason="ok",
            sentinel_path="hook", agent_identity=identity,
        )

    def test_signed_row_verifies(self) -> None:
        row = self._append(f"carryall:{self.name}#deadbeef")
        self.assertEqual(row["signer"], self.name)
        self.assertIn("sig", row)
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        self.assertIn("signed+verified", msg)

    def test_unsigned_row_still_valid(self) -> None:
        # No identity ⇒ no key ⇒ unsigned, but chain stays valid.
        row = self._append(None)
        self.assertNotIn("sig", row)
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)

    def test_unknown_identity_is_unsigned(self) -> None:
        row = self._append("carryall:ghost-agent#00000000")
        self.assertNotIn("sig", row)
        ok, _ = audit.verify_chain()
        self.assertTrue(ok)

    def test_tampered_signed_row_fails(self) -> None:
        self._append(f"carryall:{self.name}#deadbeef")
        # Flip a byte in the stored result digest without re-signing.
        p = audit.AUDIT_PATH
        import json
        lines = p.read_text().splitlines()
        entry = json.loads(lines[0])
        entry["result_digest"] = "0" * 16
        lines[0] = json.dumps(entry, ensure_ascii=False)
        p.write_text("\n".join(lines) + "\n")
        ok, msg = audit.verify_chain()
        self.assertFalse(ok)

    def test_signature_stripped_and_swapped_is_caught(self) -> None:
        # A row whose sig is present but doesn't match the signer's key.
        self._append(f"carryall:{self.name}#deadbeef")
        p = audit.AUDIT_PATH
        import json
        lines = p.read_text().splitlines()
        entry = json.loads(lines[0])
        # Corrupt only the signature bytes; hash still matches (sig excluded).
        entry["sig"] = "ab" * 64
        lines[0] = json.dumps(entry, ensure_ascii=False)
        p.write_text("\n".join(lines) + "\n")
        ok, msg = audit.verify_chain()
        self.assertFalse(ok)
        self.assertIn("signature verification FAILED", msg)

    def test_signed_but_no_pubkey_is_unverifiable_not_invalid(self) -> None:
        self._append(f"carryall:{self.name}#deadbeef")
        audit._pubkeys = {}  # drop the pubkey so verification can't run
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)
        self.assertIn("signed-unverifiable", msg)

    def test_turn_and_envelope_rows_sign(self) -> None:
        ident = f"carryall:{self.name}#deadbeef"
        audit.append_turn(
            session_id="s1", user_turn=1, model="local", route_reason="local",
            tokens_in=1, tokens_out=1, thinking_tokens=None, tool_call_count=0,
            inner_steps=1, halted_reason="model_done", agent_identity=ident,
        )
        audit.append_envelope_change(
            session_id="s1", action="grant",
            grant={"tool": "Write"}, reason="jit", agent_identity=ident,
        )
        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
