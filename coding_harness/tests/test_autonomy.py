"""Autonomy wiring: the registry brokers an 'ask' verdict when a broker is
attached and hard-denies at once otherwise; the level enum and its parser;
the REPL console broker's y/N prompt."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.context import toolprobe
from coding_harness.core.mode import Autonomy, Mode, mode_for, parse_autonomy
from coding_harness.core.permissions import PermissionBroker
from coding_harness.core.settings import Settings
from coding_harness.modes import repl_mode
from coding_harness.security import audit, hook_adapter, policy
from coding_harness.tools.bash import Bash
from coding_harness.tools.registry import ToolRegistry


class TestAutonomyEnum(unittest.TestCase):
    def test_parse_is_case_insensitive(self) -> None:
        self.assertIs(parse_autonomy("Medium"), Autonomy.MEDIUM)
        self.assertIs(parse_autonomy(" off "), Autonomy.OFF)

    def test_parse_rejects_unknown(self) -> None:
        with self.assertRaises(ValueError):
            parse_autonomy("max")

    def test_mode_for(self) -> None:
        self.assertIs(mode_for(Autonomy.OFF), Mode.PLAN)
        for level in (Autonomy.LOW, Autonomy.MEDIUM, Autonomy.HIGH):
            self.assertIs(mode_for(level), Mode.ACT)


class _RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(audit, "META_DIR", self.tmp_path),
            mock.patch.object(audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"),
            mock.patch.object(hook_adapter, "review", return_value=None),
            mock.patch.object(toolprobe, "probe", return_value={}),
            mock.patch.dict(os.environ, {"HARNESS_POLICY": ""}),
        ]
        for p in self._patches:
            p.start()
        self.events: list[tuple[str, dict]] = []

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def registry(self, **kwargs) -> ToolRegistry:
        reg = ToolRegistry(
            session_id="auto", event_sink=lambda ev: self.events.append((ev.kind, ev.payload)),
            **kwargs,
        )
        reg.register(Bash())
        return reg

    def last_audit(self) -> dict:
        lines = (self.tmp_path / "audit.jsonl").read_text().strip().splitlines()
        return json.loads(lines[-1])


class TestRegistryAsk(_RegistryCase):
    def test_ask_hard_denied_fast_without_broker(self) -> None:
        reg = self.registry()
        started = time.monotonic()
        result = reg.dispatch("Bash", {"command": "python3 script.py"})
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertTrue(result.is_error)
        self.assertTrue(result.content.startswith("BLOCKED by autonomy: autonomy_ask:"))
        self.assertTrue(result.metadata["autonomy_denied"])
        row = self.last_audit()
        self.assertFalse(row["allowed"])
        self.assertEqual(row["sentinel_path"], policy.PATH_ASK)
        self.assertTrue(row["sentinel_reason"].startswith("autonomy_ask:"))
        self.assertNotIn("tool_call_start", [k for k, _ in self.events])

    def test_ask_brokered_and_runs_on_approval(self) -> None:
        broker = PermissionBroker(emit=lambda *_: None)
        reg = self.registry(permission_broker=broker)

        def _approve() -> None:
            deadline = time.time() + 2.0
            while time.time() < deadline and not broker.list_pending():
                time.sleep(0.01)
            pending = broker.list_pending()[0]
            self.assertEqual(pending["args_preview"]["command"], "echo hi > out.txt; python3 -V")
            broker.resolve(pending["req_id"], "allow_once")

        t = threading.Thread(target=_approve, daemon=True)
        t.start()
        with tempfile.TemporaryDirectory() as wd, _chdir(wd):
            result = reg.dispatch("Bash", {"command": "echo hi > out.txt; python3 -V"})
            self.assertTrue(os.path.exists(os.path.join(wd, "out.txt")))
        t.join(timeout=2.0)
        self.assertFalse(result.is_error, result.content)
        row = self.last_audit()
        self.assertTrue(row["allowed"])
        self.assertEqual(row["sentinel_path"], policy.PATH_ASK)

    def test_ask_brokered_and_denied_on_refusal(self) -> None:
        broker = PermissionBroker(emit=lambda *_: None)
        reg = self.registry(permission_broker=broker)

        def _refuse() -> None:
            deadline = time.time() + 2.0
            while time.time() < deadline and not broker.list_pending():
                time.sleep(0.01)
            broker.resolve(broker.list_pending()[0]["req_id"], "deny")

        t = threading.Thread(target=_refuse, daemon=True)
        t.start()
        result = reg.dispatch("Bash", {"command": "python3 script.py"})
        t.join(timeout=2.0)
        self.assertTrue(result.is_error)
        self.assertIn("BLOCKED by autonomy", result.content)

    def test_deny_verdict_is_never_brokered(self) -> None:
        broker = mock.MagicMock()
        reg = self.registry(
            permission_broker=broker, settings=Settings(command_denylist=["git push"]),
            autonomy=Autonomy.HIGH,
        )
        result = reg.dispatch("Bash", {"command": "git push"})
        self.assertTrue(result.is_error)
        self.assertIn("BLOCKED by Sentinel: autonomy_denied:", result.content)
        broker.request.assert_not_called()

    def test_level_field_reaches_the_classifier(self) -> None:
        reg = self.registry(autonomy=Autonomy.MEDIUM)
        with tempfile.TemporaryDirectory() as wd, _chdir(wd):
            allowed = reg.dispatch("Bash", {"command": "mkdir -p sub"})
            self.assertFalse(allowed.is_error, allowed.content)
            asked = reg.dispatch("Bash", {"command": "curl https://example.com"})
        self.assertTrue(asked.is_error)
        self.assertIn("autonomy_ask", asked.content)


class TestConsoleBroker(_RegistryCase):
    def _registry(self) -> ToolRegistry:
        return self.registry(permission_broker=repl_mode._ConsoleBroker())

    def test_yes_runs_the_command(self) -> None:
        with mock.patch("builtins.input", return_value="y") as ask, \
                mock.patch("sys.stdin.isatty", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            result = self._registry().dispatch("Bash", {"command": "true && echo ran"})
        self.assertFalse(result.is_error, result.content)
        self.assertIn("ran", result.content)
        ask.assert_called_once()

    def test_prompt_sees_the_full_command_not_the_preview(self) -> None:
        command = "true # " + "x" * 2500
        with mock.patch.object(repl_mode.pretty, "confirm_command", return_value=True) as confirm:
            result = self._registry().dispatch("Bash", {"command": command})
        self.assertFalse(result.is_error, result.content)
        tool, args, reason = confirm.call_args.args
        self.assertEqual((tool, args["command"]), ("Bash", command))
        self.assertTrue(reason.startswith("autonomy_ask:"))

    def test_render_shows_every_line_and_defaults_to_no(self) -> None:
        buf = io.StringIO()
        command = "true\n  && echo second line"
        with mock.patch("builtins.input", return_value=""), \
                mock.patch("sys.stdin.isatty", return_value=True):
            approved = repl_mode.pretty.confirm_command("Bash", {"command": command}, "why", stream=buf)
        self.assertFalse(approved)
        self.assertIn("Bash needs approval", buf.getvalue())
        self.assertIn("| true", buf.getvalue())
        self.assertIn("|   && echo second line", buf.getvalue())

    def test_no_denies(self) -> None:
        with mock.patch("builtins.input", return_value="n"), \
                mock.patch("sys.stdin.isatty", return_value=True), contextlib.redirect_stderr(io.StringIO()):
            result = self._registry().dispatch("Bash", {"command": "true"})
        self.assertTrue(result.is_error)
        self.assertIn("BLOCKED by autonomy", result.content)

    def test_non_tty_denies_without_asking(self) -> None:
        with mock.patch("builtins.input") as ask, mock.patch("sys.stdin.isatty", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()):
            result = self._registry().dispatch("Bash", {"command": "true"})
        self.assertTrue(result.is_error)
        ask.assert_not_called()


class _chdir:
    """Context manager that switches cwd and restores it."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.prev = os.getcwd()

    def __enter__(self) -> None:
        os.chdir(self.path)

    def __exit__(self, *exc: object) -> None:
        os.chdir(self.prev)


if __name__ == "__main__":
    unittest.main()
