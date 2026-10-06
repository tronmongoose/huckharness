"""Unit tests for the deny-by-default session envelope (P1).

These are pure-logic tests — no server, no audit chain. They pin the
``check`` contract (deny-by-default, path globs, write-covers-read, expiry,
revoke) and the ``grant_for`` builder the JIT broker uses.
"""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from coding_harness.core import envelope as envelope_module
from coding_harness.core.envelope import (
    Grant,
    SessionEnvelope,
    envelope_from_dict,
    envelope_from_state_dict,
    grant_for,
    preset,
)
from coding_harness.core.mode import Autonomy
from coding_harness.core.settings import Settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


class TestEnvelopeCheck(unittest.TestCase):
    def test_empty_envelope_denies_everything(self) -> None:
        env = SessionEnvelope()
        self.assertFalse(env.check("Read", {"file_path": "/tmp/x"}).allowed)
        self.assertFalse(env.check("Bash", {"command": "ls"}).allowed)

    def test_tool_level_grant_allows_any_path(self) -> None:
        env = SessionEnvelope(grants=[Grant(tool="Read", access="read")])
        self.assertTrue(env.check("Read", {"file_path": "/anywhere"}).allowed)

    def test_path_glob_scopes_the_grant(self) -> None:
        env = SessionEnvelope(grants=[
            Grant(tool="Read", access="read", path_glob="/repo/**"),
        ])
        self.assertTrue(env.check("Read", {"file_path": "/repo/a/b.py"}).allowed)
        self.assertFalse(env.check("Read", {"file_path": "/etc/passwd"}).allowed)

    def test_prefix_dir_glob_matches_nested(self) -> None:
        env = SessionEnvelope(grants=[
            Grant(tool="Write", access="write", path_glob="/repo/src/**"),
        ])
        self.assertTrue(
            env.check("Write", {"file_path": "/repo/src/deep/x.py"}).allowed
        )

    def test_write_grant_covers_read(self) -> None:
        env = SessionEnvelope(grants=[
            Grant(tool="Read", access="write", path_glob="/repo/**"),
        ])
        # A write grant on Read's scope still satisfies a read need.
        self.assertTrue(env.check("Read", {"file_path": "/repo/x"}).allowed)

    def test_read_grant_does_not_cover_write(self) -> None:
        env = SessionEnvelope(grants=[
            Grant(tool="Write", access="read", path_glob="/repo/**"),
        ])
        self.assertFalse(env.check("Write", {"file_path": "/repo/x"}).allowed)

    def test_expired_grant_is_ignored(self) -> None:
        past = _now() - timedelta(minutes=1)
        env = SessionEnvelope(grants=[
            Grant(tool="Read", access="read", expires_at=past),
        ])
        self.assertFalse(env.check("Read", {"file_path": "/x"}).allowed)

    def test_live_grant_with_future_expiry_allows(self) -> None:
        future = _now() + timedelta(minutes=5)
        env = SessionEnvelope(grants=[
            Grant(tool="Read", access="read", expires_at=future),
        ])
        self.assertTrue(env.check("Read", {"file_path": "/x"}).allowed)

    def test_revoked_envelope_denies_all(self) -> None:
        env = SessionEnvelope(
            grants=[Grant(tool="Read", access="read")], revoked=True,
        )
        self.assertFalse(env.check("Read", {"file_path": "/x"}).allowed)
        self.assertEqual(
            env.check("Read", {"file_path": "/x"}).reason, "envelope_revoked"
        )

    def test_expired_envelope_denies_all(self) -> None:
        env = SessionEnvelope(
            grants=[Grant(tool="Read", access="read")],
            expires_at=_now() - timedelta(seconds=1),
        )
        decision = env.check("Read", {"file_path": "/x"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "envelope_expired")

    def test_bash_is_execute_tool_level_only(self) -> None:
        env = SessionEnvelope(grants=[Grant(tool="Bash", access="execute")])
        self.assertTrue(env.check("Bash", {"command": "ls -la"}).allowed)


class TestPathTraversal(unittest.TestCase):
    """Regression: a path-scoped grant must not be escapable via ``..`` or a
    symlink (the write lands where the OS resolves it, not where the string
    said). See P3 audit finding F1."""

    def test_dotdot_traversal_denied(self) -> None:
        env = SessionEnvelope(grants=[
            Grant(tool="Write", access="write", path_glob="/tmp/**"),
        ])
        # "/tmp/../etc/x" collapses to /etc/x — outside the grant.
        self.assertFalse(
            env.check("Write", {"file_path": "/tmp/../etc/cron.d/x"}).allowed
        )

    def test_legit_path_under_grant_still_allowed(self) -> None:
        env = SessionEnvelope(grants=[
            Grant(tool="Write", access="write", path_glob="/tmp/**"),
        ])
        self.assertTrue(
            env.check("Write", {"file_path": "/tmp/sub/ok.txt"}).allowed
        )

    def test_embedded_null_is_denied_not_raised(self) -> None:
        env = SessionEnvelope(grants=[Grant(tool="Write", access="write")])
        decision = env.check("Write", {"file_path": "/tmp/x\x00.py"})
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "invalid_path:embedded_null")
        self.assertTrue(env.check("Write", {"file_path": "/tmp/x.py"}).allowed)

    def test_grant_for_stores_canonical_path(self) -> None:
        g = grant_for("Write", {"file_path": "/tmp/../tmp/x"})
        # realpath collapses the round-trip; the stored glob has no "..".
        self.assertNotIn("..", g.path_glob or "")


class TestExecuteGrantBounded(unittest.TestCase):
    def test_execute_grant_gets_default_expiry(self) -> None:
        g = grant_for("Bash", {"command": "ls"})
        self.assertEqual(g.access, "execute")
        self.assertIsNotNone(g.expires_at)  # never unbounded

    def test_operator_expiry_still_honored(self) -> None:
        g = grant_for("Bash", {"command": "ls"}, expiry_minutes=1)
        self.assertIsNotNone(g.expires_at)


class TestEnvelopeValidation(unittest.TestCase):
    def test_missing_tool_raises(self) -> None:
        from coding_harness.core.envelope import EnvelopeSpecError
        with self.assertRaises(EnvelopeSpecError):
            envelope_from_dict({"grants": [{"access": "read"}]})

    def test_bad_access_raises(self) -> None:
        from coding_harness.core.envelope import EnvelopeSpecError
        with self.assertRaises(EnvelopeSpecError):
            envelope_from_dict({"grants": [{"tool": "Read", "access": "admin"}]})

    def test_non_string_path_glob_raises(self) -> None:
        from coding_harness.core.envelope import EnvelopeSpecError
        with self.assertRaises(EnvelopeSpecError):
            envelope_from_dict({"grants": [{"tool": "Read", "path_glob": ["x"]}]})

    def test_negative_expiry_raises(self) -> None:
        from coding_harness.core.envelope import EnvelopeSpecError
        with self.assertRaises(EnvelopeSpecError):
            envelope_from_dict({"grants": [{"tool": "Read"}], "expiry_minutes": -5})

    def test_is_live_reflects_revoke_and_expiry(self) -> None:
        from datetime import timedelta
        env = SessionEnvelope(grants=[Grant(tool="Read", access="read")])
        self.assertTrue(env.is_live())
        env.revoked = True
        self.assertFalse(env.is_live())
        env2 = SessionEnvelope(
            grants=[], expires_at=_now() - timedelta(seconds=1),
        )
        self.assertFalse(env2.is_live())


class TestEnvelopeFromDict(unittest.TestCase):
    def test_builds_grants_and_expiry(self) -> None:
        env = envelope_from_dict({
            "grants": [
                {"tool": "Read", "access": "read", "path_glob": "/r/**"},
                {"tool": "Write", "access": "write", "path_glob": "/r/out/**",
                 "expiry_minutes": 10},
            ],
            "expiry_minutes": 60,
        })
        self.assertEqual(len(env.grants), 2)
        self.assertIsNotNone(env.expires_at)
        self.assertIsNotNone(env.grants[1].expires_at)

    def test_missing_access_defaults_to_read(self) -> None:
        env = envelope_from_dict({"grants": [{"tool": "Read"}]})
        self.assertEqual(env.grants[0].access, "read")


class TestGrantFor(unittest.TestCase):
    def test_grant_for_write_captures_path_and_access(self) -> None:
        g = grant_for("Write", {"file_path": "/repo/x.py"}, expiry_minutes=5)
        self.assertEqual(g.tool, "Write")
        self.assertEqual(g.access, "write")
        self.assertEqual(g.path_glob, "/repo/x.py")
        self.assertEqual(g.granted_by, "operator")
        self.assertIsNotNone(g.expires_at)

    def test_grant_for_bash_is_tool_level(self) -> None:
        g = grant_for("Bash", {"command": "ls"})
        self.assertEqual(g.access, "execute")
        self.assertIsNone(g.path_glob)

    def test_grant_for_then_check_allows(self) -> None:
        g = grant_for("Read", {"file_path": "/repo/a.py"})
        env = SessionEnvelope(grants=[g])
        self.assertTrue(env.check("Read", {"file_path": "/repo/a.py"}).allowed)


class TestPreset(unittest.TestCase):
    def test_preset_shape(self) -> None:
        root = os.path.realpath("/tmp/repo")
        env = preset("/tmp/repo")
        by_tool: dict[str, list[Grant]] = {}
        for g in env.grants:
            by_tool.setdefault(g.tool, []).append(g)
        self.assertEqual(
            set(by_tool), {"Read", "Grep", "Glob", "Write", "Edit", "Bash"},
        )
        for tool in ("Read", "Grep", "Glob"):
            self.assertEqual(by_tool[tool][0].access, "read")
            self.assertEqual(by_tool[tool][0].path_glob, f"{root}/**")
        for tool in ("Write", "Edit"):
            self.assertEqual(by_tool[tool][0].access, "write")
            self.assertEqual(by_tool[tool][0].path_glob, f"{root}/**")
        bash = by_tool["Bash"][0]
        self.assertEqual(bash.access, "execute")
        self.assertIsNone(bash.path_glob)
        self.assertIsNone(bash.expires_at)
        for g in env.grants:
            self.assertEqual(g.granted_by, "default")
            self.assertIsNone(g.expires_at)
        self.assertIsNone(env.expires_at)

    def test_extra_read_roots_add_read_only_grants(self) -> None:
        other = os.path.realpath("/tmp/other")
        env = preset("/tmp/repo", settings=Settings(extra_read_roots=["/tmp/other"]))
        reads = {g.tool for g in env.grants if g.path_glob == f"{other}/**"}
        self.assertEqual(reads, {"Read", "Grep", "Glob"})
        self.assertTrue(env.check("Read", {"file_path": "/tmp/other/x"}).allowed)
        self.assertFalse(env.check("Write", {"file_path": "/tmp/other/x"}).allowed)

    def test_off_level_grants_reads_only(self) -> None:
        env = preset("/tmp/repo", Autonomy.OFF)
        self.assertEqual({g.tool for g in env.grants}, {"Read", "Grep", "Glob"})
        self.assertTrue(env.check("Read", {"file_path": "/tmp/repo/a"}).allowed)
        self.assertFalse(env.check("Write", {"file_path": "/tmp/repo/a"}).allowed)
        self.assertFalse(env.check("Bash", {"command": "ls"}).allowed)

    def test_low_and_above_grant_writes_and_bash(self) -> None:
        for level in (Autonomy.LOW, Autonomy.MEDIUM, Autonomy.HIGH):
            with self.subTest(level=level.value):
                env = preset("/tmp/repo", level)
                self.assertEqual(
                    {g.tool for g in env.grants},
                    {"Read", "Grep", "Glob", "Write", "Edit", "Bash"},
                )
                self.assertTrue(env.check("Write", {"file_path": "/tmp/repo/a"}).allowed)
                self.assertTrue(env.check("Bash", {"command": "ls"}).allowed)

    def test_deny_write_roots_close_writes_inside_cwd(self) -> None:
        env = preset("/tmp/repo", Autonomy.LOW, Settings(deny_write=["/tmp/repo/secrets"]))
        self.assertEqual(env.deny_write, [os.path.realpath("/tmp/repo/secrets")])
        decision = env.check("Write", {"file_path": "/tmp/repo/secrets/k"})
        self.assertFalse(decision.allowed)
        self.assertTrue(decision.reason.startswith("deny_write:"))
        self.assertTrue(env.check("Read", {"file_path": "/tmp/repo/secrets/k"}).allowed)
        self.assertTrue(env.check("Write", {"file_path": "/tmp/repo/other"}).allowed)
        self.assertFalse(env.check("Edit", {"file_path": "/tmp/repo/secrets"}).allowed)

    def test_deny_write_survives_the_state_round_trip(self) -> None:
        env = preset("/tmp/repo", Autonomy.LOW, Settings(deny_write=["/tmp/repo/secrets"]))
        again = envelope_from_state_dict(env.to_dict())
        self.assertEqual(again.deny_write, env.deny_write)
        self.assertFalse(again.check("Write", {"file_path": "/tmp/repo/secrets/k"}).allowed)

    def test_preset_bash_grant_live_after_20_minutes(self) -> None:
        env = preset("/tmp/repo")
        later = _now() + timedelta(minutes=20)
        bash = next(g for g in env.grants if g.tool == "Bash")
        self.assertFalse(bash.is_expired(later))
        with mock.patch.object(envelope_module, "datetime") as dt:
            dt.now.return_value = later
            self.assertTrue(env.check("Bash", {"command": "ls"}).allowed)
        # Contrast: the allow_always execute grant is bounded by then.
        self.assertTrue(grant_for("Bash", {"command": "ls"}).is_expired(later))


class TestCategoryFallback(unittest.TestCase):
    def test_nameless_read_tool_passes_on_preset(self) -> None:
        env = preset(os.getcwd())
        decision = env.check("carryall_list_vaults", {}, category="read")
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "grant_category_read")

    def test_no_category_means_no_fallback(self) -> None:
        env = preset(os.getcwd())
        self.assertFalse(env.check("carryall_list_vaults", {}).allowed)

    def test_read_fallback_requires_a_grant_covering_cwd(self) -> None:
        elsewhere = SessionEnvelope(grants=[
            Grant(tool="Read", access="read", path_glob="/nowhere/**"),
        ])
        self.assertFalse(
            elsewhere.check("carryall_list_vaults", {}, category="read").allowed
        )
        tool_level = SessionEnvelope(grants=[Grant(tool="Read", access="read")])
        self.assertTrue(
            tool_level.check("carryall_list_vaults", {}, category="read").allowed
        )

    def test_read_tool_with_path_outside_scope_gets_no_fallback(self) -> None:
        env = preset(os.getcwd())
        self.assertFalse(
            env.check("Read", {"file_path": "/etc/hosts"}, category="read").allowed
        )

    def test_grep_without_path_passes_on_preset(self) -> None:
        env = preset(os.getcwd())
        self.assertTrue(env.check("Grep", {"pattern": "x"}, category="read").allowed)

    def test_mcp_execute_tool_denied_unless_named(self) -> None:
        env = preset(os.getcwd())
        args = {"doc": "x"}
        self.assertFalse(
            env.check("carryall_write_document", args, category="execute").allowed
        )
        env.add_grant(Grant(tool="carryall_write_document", access="execute"))
        self.assertTrue(
            env.check("carryall_write_document", args, category="execute").allowed
        )

    def test_mcp_execute_tool_needs_execute_access(self) -> None:
        env = SessionEnvelope(grants=[
            Grant(tool="carryall_write_document", access="read"),
        ])
        self.assertFalse(
            env.check("carryall_write_document", {}, category="execute").allowed
        )

    def test_mcp_edit_category_needs_write_access(self) -> None:
        env = SessionEnvelope(grants=[Grant(tool="some_editor", access="read")])
        self.assertFalse(env.check("some_editor", {}, category="edit").allowed)
        env.add_grant(Grant(tool="some_editor", access="write"))
        self.assertTrue(env.check("some_editor", {}, category="edit").allowed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
