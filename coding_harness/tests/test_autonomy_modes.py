"""Autonomy at the entry points: cli flags and the --mode alias, print mode's
level and preset, the REPL /autonomy command, and the serve create body."""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from coding_harness import cli
from coding_harness.core import session as session_module
from coding_harness.core.mode import Autonomy, Mode
from coding_harness.core.settings import Settings, SettingsError
from coding_harness.modes import print_mode, repl_mode, serve_mode
from coding_harness.security import audit
from coding_harness.tests.test_serve_mode import _request, _start_server


def _fake_chat(*, model, messages, tools, on_delta=None, timeout=None, **_kw):  # type: ignore[no-untyped-def]
    return {
        "role": "assistant",
        "content": "done",
        "tool_calls": [],
        "_usage": {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None},
    }


class _ModeCase(unittest.TestCase):
    """Audit, session log and model patched so a mode entry point runs hermetically."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(self.tmp.name)
        self._patches = [
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(session_module, "SESSIONS_DIR", tmp_path / "sessions"),
            mock.patch("coding_harness.core.session.ollama.chat", side_effect=_fake_chat),
            mock.patch.dict(os.environ, {
                "HARNESS_REPO_MAP": "0", "HARNESS_TOOL_PROBE": "0",
                "HARNESS_SETTINGS": "off", "HARNESS_ENVELOPE": "",
            }),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def audit_kinds(self) -> list[str]:
        path = audit.AUDIT_PATH
        if not path.exists():
            return []
        return [json.loads(line).get("kind") for line in path.read_text().splitlines() if line]


class TestCliFlags(unittest.TestCase):
    def _print_kwargs(self, argv: list[str]) -> dict:
        with mock.patch("coding_harness.cli.print_mode.run", return_value=0) as run, \
                mock.patch.dict(os.environ, {"HARNESS_SETTINGS": "off"}):
            self.assertEqual(cli.main([*argv, "--no-mcp", "hi"]), 0)
        return run.call_args.kwargs

    def test_autonomy_flag(self) -> None:
        self.assertIs(self._print_kwargs(["--autonomy", "medium"])["autonomy"], Autonomy.MEDIUM)

    def test_default_leaves_the_level_to_settings(self) -> None:
        kwargs = self._print_kwargs([])
        self.assertIsNone(kwargs["autonomy"])
        self.assertEqual(kwargs["settings"], Settings())

    def test_mode_alias(self) -> None:
        self.assertIs(self._print_kwargs(["--mode", "plan"])["autonomy"], Autonomy.OFF)
        self.assertIs(self._print_kwargs(["--mode", "act"])["autonomy"], Autonomy.LOW)

    def test_autonomy_wins_over_mode(self) -> None:
        self.assertIs(
            self._print_kwargs(["--mode", "plan", "--autonomy", "high"])["autonomy"],
            Autonomy.HIGH,
        )

    def test_repl_receives_the_level(self) -> None:
        with mock.patch("coding_harness.cli.repl_mode.run", return_value=0) as run, \
                mock.patch.dict(os.environ, {"HARNESS_SETTINGS": "off"}):
            self.assertEqual(cli.main(["-i", "--autonomy", "off", "--no-mcp"]), 0)
        self.assertIs(run.call_args.kwargs["autonomy"], Autonomy.OFF)

    def test_bad_settings_file_exits_2(self) -> None:
        err = io.StringIO()
        with mock.patch("coding_harness.cli.load_settings", side_effect=SettingsError("x: unknown settings key 'foo'")), \
                mock.patch("coding_harness.cli.print_mode.run", return_value=0) as run, \
                contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--no-mcp", "hi"]), 2)
        run.assert_not_called()
        self.assertIn("foo", err.getvalue())


class TestPrintModeAutonomy(_ModeCase):
    def _session_start(self, **kwargs) -> dict:
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = print_mode.run("hello", enable_mcp=False, force_local=True, **kwargs)
        self.assertEqual(code, 0)
        events = [json.loads(line) for line in err.getvalue().splitlines() if line.startswith("{")]
        return next(e for e in events if e["event"] == "session_start")

    def test_default_is_low_and_act(self) -> None:
        start = self._session_start()
        self.assertEqual(start["autonomy"], "low")
        self.assertEqual(start["mode"], "act")
        self.assertIn("Bash", start["tools"])

    def test_off_is_plan_with_read_grants_only(self) -> None:
        start = self._session_start(autonomy=Autonomy.OFF)
        self.assertEqual(start["autonomy"], "off")
        self.assertEqual(start["mode"], "plan")
        self.assertEqual(set(start["tools"]), {"Read", "Grep", "Glob", "TodoWrite"})
        self.assertEqual({g["tool"] for g in start["envelope"]["grants"]}, {"Read", "Grep", "Glob"})

    def test_settings_level_applies_without_a_flag(self) -> None:
        start = self._session_start(settings=Settings(autonomy=Autonomy.MEDIUM))
        self.assertEqual(start["autonomy"], "medium")
        start = self._session_start(settings=Settings(autonomy=Autonomy.MEDIUM), autonomy=Autonomy.HIGH)
        self.assertEqual(start["autonomy"], "high")


class TestReplAutonomy(_ModeCase):
    def _run(self, lines: list[str], **kwargs) -> str:
        err = io.StringIO()
        with mock.patch("builtins.input", side_effect=[*lines, EOFError()]), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = repl_mode.run(enable_mcp=False, force_local=True, **kwargs)
        self.assertEqual(code, 0)
        return err.getvalue()

    def test_show_change_and_audit(self) -> None:
        out = self._run(["/autonomy", "/autonomy medium", "/autonomy MEDIUM", "/autonomy bogus"])
        self.assertIn("autonomy low · mode act", out)
        self.assertIn("autonomy → medium · mode act", out)
        self.assertIn("/autonomy: unknown autonomy level 'bogus'", out)
        self.assertEqual(self.audit_kinds().count("autonomy_change"), 1)

    def test_off_switches_to_plan_and_back(self) -> None:
        out = self._run(["/autonomy off", "/autonomy low"])
        self.assertIn("autonomy → off · mode plan", out)
        self.assertIn("autonomy → low · mode act", out)
        kinds = self.audit_kinds()
        self.assertEqual(kinds.count("mode_change"), 2)
        self.assertEqual(kinds.count("autonomy_change"), 2)

    def test_repl_starts_at_the_requested_level(self) -> None:
        out = self._run(["/autonomy"], autonomy=Autonomy.HIGH)
        self.assertIn("autonomy high · mode act", out)


class TestServeAutonomy(_ModeCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server, cls.thread, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)

    def _create(self, body: dict) -> tuple[int, dict]:
        status, resp = _request("POST", f"http://127.0.0.1:{self.port}/v1/sessions", body=body)
        return status, json.loads(resp)

    def test_default_body_is_plan_at_low(self) -> None:
        status, payload = self._create({})
        self.assertEqual(status, 201)
        self.assertEqual((payload["mode"], payload["autonomy"]), ("plan", "low"))

    def test_act_body_runs_at_low(self) -> None:
        _, payload = self._create({"mode": "act"})
        self.assertEqual((payload["mode"], payload["autonomy"]), ("act", "low"))

    def test_explicit_level_decides_the_mode(self) -> None:
        _, payload = self._create({"autonomy": "off"})
        self.assertEqual((payload["mode"], payload["autonomy"]), ("plan", "off"))
        self.assertEqual({g["tool"] for g in payload["envelope"]["grants"]}, {"Read", "Grep", "Glob"})
        _, payload = self._create({"mode": "plan", "autonomy": "Medium"})
        self.assertEqual((payload["mode"], payload["autonomy"]), ("act", "medium"))

    def test_bad_level_400(self) -> None:
        status, payload = self._create({"autonomy": "max"})
        self.assertEqual(status, 400)
        self.assertIn("autonomy", payload["error"]["message"])
        status, _ = self._create({"autonomy": 3})
        self.assertEqual(status, 400)

    def test_headless_act_turn_denies_an_ask_and_completes(self) -> None:
        calls = {"n": 0}

        def _chat(*, model, messages, tools, on_delta=None, timeout=None, **_kw):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] > 1:
                return _fake_chat(model=model, messages=messages, tools=tools)
            return {
                "role": "assistant", "content": "",
                "tool_calls": [{"function": {"name": "Bash", "arguments": {"command": "python3 script.py"}}}],
                "_usage": {"tokens_in": 0, "tokens_out": 0, "thinking_tokens": None},
            }

        _, created = self._create({"mode": "act"})
        sid = created["session_id"]
        base = f"http://127.0.0.1:{self.port}/v1/sessions/{sid}"
        started = time.monotonic()
        with mock.patch("coding_harness.core.session.ollama.chat", side_effect=_chat):
            status, resp = _request("POST", f"{base}/turn", body={"message": "run the script", "wait": True})
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(json.loads(resp)["halted_reason"], "model_done")
        _, audit_resp = _request("GET", f"{base}/audit")
        rows = [r for r in json.loads(audit_resp)["entries"] if r.get("tool") == "Bash"]
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["allowed"])
        self.assertEqual(rows[0]["sentinel_path"], "policy:v1:ask")
        self.assertTrue(rows[0]["sentinel_reason"].startswith("autonomy_ask:"))

    def test_session_start_logs_the_level(self) -> None:
        state = serve_mode._ServerState(
            model="test-model", force_local=True, explicit_model=False, enable_mcp=False,
            settings=Settings(),
        )
        entry = state.create_session(mode=Mode.PLAN, autonomy=Autonomy.MEDIUM)
        self.assertIs(entry.session.mode, Mode.ACT)
        entry.session.run_turn("hi")
        records = [json.loads(line) for line in entry.session.session_log_path.read_text().splitlines()]
        start = next(r for r in records if r["kind"] == "session_start")
        self.assertEqual(start["autonomy"], "medium")
        entry.session.close()
        listed = state.list_sessions()[0]
        self.assertEqual(listed["autonomy"], "medium")


if __name__ == "__main__":
    unittest.main()
