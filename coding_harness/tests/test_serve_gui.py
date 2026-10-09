"""Serve-mode surface the GUI depends on: replay, per-session model, launcher.

Reuses the real-server harness from test_serve_mode; the model call is faked.
"""
from __future__ import annotations

import json
import tempfile
import unittest
import urllib.request
from collections import deque
from pathlib import Path
from unittest import mock

from coding_harness import cli
from coding_harness.context import skill_buckets
from coding_harness.context import skills as skills_mod
from coding_harness.modes import serve_gui, serve_mode, ui_mode
from coding_harness.security import audit
from coding_harness.tests.test_serve_mode import (
    _fake_ollama_chat,
    _request,
    _start_server,
)


def _read_events(url: str, until: str, limit: int = 200) -> list[dict]:
    """Read SSE notifications from ``url`` until one with method ``until``."""
    events: list[dict] = []
    with urllib.request.urlopen(urllib.request.Request(url), timeout=5.0) as resp:
        for _ in range(limit):
            line = resp.readline().decode("utf-8")
            if not line.startswith("data: "):
                continue
            events.append(json.loads(line[len("data: "):]))
            if events[-1]["method"] == until:
                return events
    raise AssertionError(f"never saw {until}: {[e['method'] for e in events]}")


class TestServeGui(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        tmp_path = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(audit, "AUDIT_PATH", tmp_path / "audit.jsonl"),
            mock.patch.object(audit, "ANCHORS_PATH", tmp_path / "anchors.jsonl"),
            mock.patch.object(audit, "META_DIR", tmp_path),
            mock.patch.object(skill_buckets, "USER_MAP",
                              tmp_path / "no-config" / "skill_buckets.json"),
            mock.patch("coding_harness.core.session.ollama.chat",
                       side_effect=_fake_ollama_chat),
            mock.patch("coding_harness.tools.registry.sentinel.review",
                       return_value=mock.MagicMock(allowed=True, reason="t", path="hook")),
        ]
        for p in cls.patches:
            p.start()
        cls.server, cls.thread, cls.port = _start_server()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2.0)
        for p in cls.patches:
            p.stop()
        cls.tmp.cleanup()

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _session_after_turn(self, message: str) -> str:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/turn"),
                             body={"message": message, "wait": True})
        self.assertEqual(status, 200)
        return sid

    def _summary(self, sid: str) -> dict:
        _, body = _request("GET", self._url("/v1/sessions"))
        return next(s for s in json.loads(body)["sessions"] if s["session_id"] == sid)

    def test_late_listener_replays_the_finished_turn(self) -> None:
        sid = self._session_after_turn("replay me")
        url = self._url(f"/v1/sessions/{sid}/events")
        for _ in range(2):  # a second tab must see the same, unmutated history
            events = _read_events(url, until="turn_done")
            methods = [e["method"] for e in events]
            self.assertIn("turn_start", methods)
            deltas = [e for e in events if e["method"] == "assistant_delta"]
            self.assertEqual(len(deltas), 1, "consecutive deltas are folded")
            self.assertEqual(deltas[0]["params"]["text"].strip(), "echo: replay me")
            self.assertNotIn("event", events[0]["params"])

    def test_title_is_the_first_prompt_line(self) -> None:
        sid = self._session_after_turn("fix the parser\nsecond line")
        self.assertEqual(self._summary(sid)["title"], "fix the parser")

    def test_set_model_pins_and_auto_releases(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        path = self._url(f"/v1/sessions/{sid}/model")
        status, body = _request("POST", path, body={"model": "gpt-oss:20b"})
        self.assertEqual(status, 200, body)
        summary = self._summary(sid)
        self.assertEqual(summary["model"], "gpt-oss:20b")
        self.assertTrue(summary["explicit_model"])
        status, _ = _request("POST", path, body={"model": "auto"})
        self.assertEqual(status, 200)
        self.assertFalse(self._summary(sid)["explicit_model"])

    def test_set_model_refuses_banned_origin(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/model"),
                             body={"model": "qwen2.5:7b"})
        self.assertEqual(status, 400)

    def test_healthz_names_the_project(self) -> None:
        _, body = _request("GET", self._url("/v1/healthz"))
        payload = json.loads(body)
        self.assertTrue(payload["cwd"])
        self.assertIn(payload["default_autonomy"], ("off", "low", "medium", "high"))

    def test_models_list_drops_embedding_models(self) -> None:
        tags = json.dumps({"models": [{"name": "gpt-oss:20b"},
                                      {"name": "nomic-embed-text:latest"}]}).encode()
        resp = mock.MagicMock()
        resp.__enter__.return_value.read.return_value = tags
        with mock.patch("coding_harness.modes.ops.urlopen", return_value=resp):
            _, body = _request("GET", self._url("/v1/models"))
        ids = [m["id"] for m in json.loads(body)["models"]]
        self.assertIn("gpt-oss:20b", ids)
        self.assertNotIn("nomic-embed-text:latest", ids)

    def test_turn_announces_the_model_read_and_reports_context(self) -> None:
        sid = self._session_after_turn("ctx")
        events = _read_events(self._url(f"/v1/sessions/{sid}/events"), until="turn_done")
        starts = [e["params"] for e in events if e["method"] == "model_call_start"]
        self.assertEqual(starts[0]["step"], 1)
        self.assertEqual(starts[0]["backend"], "ollama")
        done = events[-1]["params"]
        self.assertGreater(done["num_ctx"], 0)
        self.assertIn("prompt_tokens", done)
        # A question that never edits takes no test baseline.
        self.assertNotIn("checks_baseline_start", [e["method"] for e in events])

    def test_ollama_residency_and_loaded_flag(self) -> None:
        ps = json.dumps({"models": [{"name": "gpt-oss:20b", "size": 14 * 2**30}]}).encode()
        tags = json.dumps({"models": [{"name": "gpt-oss:20b"}, {"name": "phi4-mini:latest"}]}).encode()

        def fake(url, timeout=None):
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = ps if url.endswith("/api/ps") else tags
            return resp

        with mock.patch("coding_harness.modes.ops.urlopen", side_effect=fake):
            _, body = _request("GET", self._url("/v1/ollama"))
            self.assertEqual(json.loads(body)["resident"][0],
                             {"model": "gpt-oss:20b", "size_gb": 14.0, "until": None})
            _, body = _request("GET", self._url("/v1/models"))
        loaded = {m["id"]: m.get("loaded") for m in json.loads(body)["models"]}
        self.assertEqual((loaded["gpt-oss:20b"], loaded["phi4-mini:latest"]), (True, False))

    def test_ollama_down_is_reported_not_raised(self) -> None:
        with mock.patch("coding_harness.modes.ops.urlopen", side_effect=OSError("refused")):
            _, body = _request("GET", self._url("/v1/ollama"))
        self.assertEqual(json.loads(body), {"reachable": False, "resident": []})

    def _fake_skills(self, tmp: Path):
        pack = tmp / "tidy"
        pack.mkdir()
        (pack / "SKILL.md").write_text("---\nname: tidy\ndescription: keep it neat\n---\nBe neat.\n")
        skill = skills_mod.Skill("tidy", "keep it neat", pack / "SKILL.md")
        return mock.patch.object(skills_mod, "index_skills", return_value=[skill])

    def test_skills_endpoint_lists_the_index(self) -> None:
        with tempfile.TemporaryDirectory() as d, self._fake_skills(Path(d)):
            _, body = _request("GET", self._url("/v1/skills"))
        listed = json.loads(body)
        self.assertEqual(listed["skills"], [{"name": "tidy", "description": "keep it neat",
                                             "source": "project", "bucket": "Other"}])
        self.assertEqual(listed["buckets"][-1], {"name": "Other", "count": 1})

    def test_skill_turn_carries_the_skill_and_titles_the_session(self) -> None:
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        with tempfile.TemporaryDirectory() as d, self._fake_skills(Path(d)):
            status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/turn"),
                                 body={"skill": "tidy", "message": "the desk", "wait": True})
            self.assertEqual(status, 200)
            status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/turn"),
                                 body={"skill": "nope", "message": "x"})
            self.assertEqual(status, 404)
        events = _read_events(self._url(f"/v1/sessions/{sid}/events"), until="turn_done")
        prompt = next(e for e in events if e["method"] == "turn_start")["params"]["prompt"]
        self.assertTrue(prompt.startswith("[skill: tidy]\n---"))
        self.assertTrue(prompt.endswith("Task: the desk"))
        self.assertEqual(self._summary(sid)["title"], "tidy: the desk")

    def _write_transcript(self, sid: str, cwd: str, prompt: str = "old question") -> None:
        rows = [
            {"kind": "session_start", "session_id": sid, "model": "m", "cwd": cwd},
            {"kind": "user_message", "content": prompt, "turn": 1},
            {"kind": "turn_start", "turn": 1, "prompt": prompt},
            {"kind": "assistant_message", "turn": 1, "content": "",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "Bash", "arguments": "{\"command\": \"ls\"}"}}]},
            {"kind": "tool_result", "turn": 1, "tool_call_id": "c1", "name": "Bash",
             "content": "a.py", "is_error": False},
            {"kind": "assistant_message", "turn": 2, "content": "old answer"},
            {"kind": "turn_done", "turn": 1, "halted_reason": "model_done"},
        ]
        path = serve_mode.SESSIONS_DIR / f"{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def test_history_lists_past_sessions_here_and_resume_rebuilds_the_thread(self) -> None:
        import os
        self._write_transcript("20200101T000000-aaaaaaaa", os.getcwd())
        self._write_transcript("20200101T000000-bbbbbbbb", "/some/other/repo")
        _, body = _request("GET", self._url("/v1/transcripts"))
        listed = {t["session_id"]: t["title"] for t in json.loads(body)["transcripts"]}
        self.assertEqual(listed.get("20200101T000000-aaaaaaaa"), "old question")
        self.assertNotIn("20200101T000000-bbbbbbbb", listed)

        status, body = _request("POST", self._url("/v1/sessions/20200101T000000-aaaaaaaa/resume"),
                                body={"interactive": True})
        self.assertEqual(status, 200, body)
        events = _read_events(self._url("/v1/sessions/20200101T000000-aaaaaaaa/events"),
                              until="turn_done")
        methods = [e["method"] for e in events]
        self.assertEqual(methods, ["turn_start", "tool_call_start", "tool_call_result",
                                   "assistant_delta", "turn_done"])  # session_resumed follows
        self.assertEqual(events[1]["params"]["args"], {"command": "ls"})
        self.assertEqual(self._summary("20200101T000000-aaaaaaaa")["title"], "old question")
        _, body = _request("GET", self._url("/v1/transcripts"))
        live_now = {t["session_id"] for t in json.loads(body)["transcripts"]}
        self.assertNotIn("20200101T000000-aaaaaaaa", live_now, "a live session is not history")

    def test_resume_refuses_another_projects_session(self) -> None:
        self._write_transcript("20200101T000000-cccccccc", "/some/other/repo")
        status, body = _request("POST", self._url("/v1/sessions/20200101T000000-cccccccc/resume"),
                                body={})
        self.assertEqual(status, 409)
        self.assertIn("other_project", body)

    def test_compact_reports_the_fold(self) -> None:
        from coding_harness.core.compaction import CompactionResult
        _, create = _request("POST", self._url("/v1/sessions"), body={})
        sid = json.loads(create)["session_id"]
        folded = CompactionResult([], True, "ok", 12, 4, 900, 300)
        with mock.patch("coding_harness.core.session.Session.compact", return_value=folded):
            status, body = _request("POST", self._url(f"/v1/sessions/{sid}/compact"), body={})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["messages_after"], 4)

    def _projects_root(self, tmp: Path):
        for name in ("alpha", "Beta"):
            (tmp / name / ".git").mkdir(parents=True)
        (tmp / "alpha-sl-1").mkdir()
        (tmp / "alpha-sl-1" / ".git").write_text("gitdir: elsewhere\n")  # a worktree
        return mock.patch.object(serve_gui, "PROJECTS_ROOT", tmp)

    def test_projects_lists_main_checkouts_only(self) -> None:
        with tempfile.TemporaryDirectory() as d, self._projects_root(Path(d)):
            _, body = _request("GET", self._url("/v1/projects"))
        names = [p["name"] for p in json.loads(body)["projects"]]
        self.assertEqual(names, ["alpha", "Beta"])

    def test_open_refuses_a_path_that_is_not_listed(self) -> None:
        with tempfile.TemporaryDirectory() as d, self._projects_root(Path(d)):
            status, _ = _request("POST", self._url("/v1/projects/open"), body={"path": "/etc"})
        self.assertEqual(status, 400)

    def test_open_reuses_a_live_server_and_reports_one_that_never_starts(self) -> None:
        import os
        with tempfile.TemporaryDirectory() as d, self._projects_root(Path(d)):
            alpha, beta = str(Path(d) / "alpha"), str(Path(d) / "Beta")
            ui_mode.registry_dir().mkdir(parents=True, exist_ok=True)
            (ui_mode.registry_dir() / "alpha.json").write_text(
                json.dumps({"pid": os.getpid(), "port": 5999, "cwd": alpha}))
            try:
                status, body = _request("POST", self._url("/v1/projects/open"), body={"path": alpha})
                self.assertEqual((status, json.loads(body)),
                                 (200, {"url": "http://127.0.0.1:5999", "status": "running"}))
                with mock.patch.object(ui_mode, "spawn", return_value=None) as spawned:
                    status, _ = _request("POST", self._url("/v1/projects/open"), body={"path": beta})
                self.assertEqual(status, 502)
                self.assertEqual(spawned.call_args.args, (beta,))
            finally:
                (ui_mode.registry_dir() / "alpha.json").unlink(missing_ok=True)

    def _skill_roots(self, tmp: Path):
        borrowed = tmp / "claude-skills" / "tenets"
        borrowed.mkdir(parents=True)
        (borrowed / "SKILL.md").write_text("---\nname: tenets\ndescription: house rules\n---\nBe plain.\n")
        return (mock.patch.object(skills_mod, "SKILLS_USER", tmp / "bjorn-skills"),
                mock.patch.object(skills_mod, "SKILLS_USER_EXTRA", (tmp / "claude-skills",)))

    def test_skill_save_then_read_back_as_editable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a, b = self._skill_roots(Path(d))
            with a, b:
                status, body = _request("POST", self._url("/v1/skills/weekly-review"), body={
                    "description": "run the weekly review", "body": "1. Read the log."})
                self.assertEqual(status, 200, body)
                _, body = _request("GET", self._url("/v1/skills/weekly-review"))
                detail = json.loads(body)
                self.assertEqual((detail["editable"], detail["source"]), (True, "bjorn"))
                self.assertIn("1. Read the log.", detail["text"])
                status, _ = _request("POST", self._url("/v1/skills/filed"), body={
                    "description": "x", "body": "y", "category": "Fleet & ops"})
                self.assertEqual(status, 200)
                _, body = _request("GET", self._url("/v1/skills/filed"))
                self.assertEqual(json.loads(body)["bucket"], "Fleet & ops")
                for bad in ("Gardening", 7):
                    status, _ = _request("POST", self._url("/v1/skills/odd"), body={
                        "description": "x", "body": "y", "category": bad})
                    self.assertEqual(status, 400, bad)

    def test_borrowed_skills_are_read_only_and_names_are_confined(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            a, b = self._skill_roots(Path(d))
            with a, b:
                _, body = _request("GET", self._url("/v1/skills/tenets"))
                self.assertFalse(json.loads(body)["editable"])
                status, _ = _request("POST", self._url("/v1/skills/tenets"),
                                     body={"description": "x", "body": "y"})
                self.assertEqual(status, 409)
                for bad in ("..%2Fescape", "Upper", "a%20b"):
                    status, _ = _request("POST", self._url(f"/v1/skills/{bad}"),
                                         body={"description": "x", "body": "y"})
                    self.assertEqual(status, 400, bad)
                self.assertFalse((Path(d) / "escape").exists())

    def test_chat_kind_on_create_and_switch_back_to_code(self) -> None:
        from coding_harness.core import model_roles
        with mock.patch.object(model_roles, "_present", lambda tag: True):
            _, create = _request("POST", self._url("/v1/sessions"), body={"kind": "chat"})
            created = json.loads(create)
            self.assertEqual((created["kind"], created["model"]), ("chat", model_roles.SMALL))
            sid = created["session_id"]
            status, body = _request("POST", self._url(f"/v1/sessions/{sid}/kind"), body={"kind": "code"})
            self.assertEqual(status, 200, body)
            self.assertEqual(self._summary(sid)["kind"], "code")
            status, _ = _request("POST", self._url(f"/v1/sessions/{sid}/kind"), body={"kind": "poem"})
            self.assertEqual(status, 400)

    def test_role_table_lists_every_role(self) -> None:
        from coding_harness.core import model_roles
        with mock.patch.object(model_roles, "_present", lambda tag: True):
            _, body = _request("GET", self._url("/v1/models/roles"))
        roles = {r["role"]: r for r in json.loads(body)["roles"]}
        self.assertEqual(set(roles), set(model_roles.ROLES))
        self.assertEqual(roles["explore"]["model"], model_roles.SMALL)


def test_registry_drops_entries_whose_process_died(tmp_path) -> None:
    with mock.patch.object(ui_mode, "registry_dir", return_value=tmp_path):
        (tmp_path / "gone.json").write_text(json.dumps({"pid": 2**22 + 7, "port": 1, "cwd": "/x"}))
        assert ui_mode.live_servers() == {}
        assert not (tmp_path / "gone.json").exists()


def test_launcher_registers_while_serving_and_unregisters_after(tmp_path) -> None:
    seen: dict = {}

    def _fake_run(**kw):
        kw["ready_callback"](mock.MagicMock(server_port=4321))
        seen.update(ui_mode.live_servers())
        return 0

    with mock.patch.object(ui_mode, "registry_dir", return_value=tmp_path), \
            mock.patch.object(serve_mode, "run", side_effect=_fake_run), \
            mock.patch.object(ui_mode.webbrowser, "open"):
        ui_mode.run(model="m", explicit_model=False, force_local=False,
                    enable_mcp=False, settings=mock.MagicMock())
        assert list(seen.values()) == ["http://127.0.0.1:4321"]
        assert ui_mode.live_servers() == {}


def test_remember_folds_only_adjacent_deltas() -> None:
    history: deque[dict] = deque(maxlen=10)
    for payload in ({"event": "assistant_delta", "text": "a"},
                    {"event": "assistant_delta", "text": "b"},
                    {"event": "tool_call_start", "tool": "Read"},
                    {"event": "assistant_delta", "text": "c"}):
        serve_mode._remember(history, payload)
    assert [p.get("text") for p in history] == ["ab", None, "c"]


def test_bare_bjorn_opens_the_gui() -> None:
    with mock.patch.object(ui_mode, "run", return_value=0) as run:
        assert cli.main(["--no-open", "--port", "0"]) == 0
    assert run.call_args.kwargs["open_browser"] is False
    assert run.call_args.kwargs["port"] == 0


def test_launcher_opens_a_new_session_url() -> None:
    server = mock.MagicMock(server_port=4321)

    def _fake_run(**kw):
        kw["ready_callback"](server)
        return 0

    with mock.patch.object(serve_mode, "run", side_effect=_fake_run), \
            mock.patch.object(ui_mode.webbrowser, "open") as opened:
        ui_mode.run(model="m", explicit_model=False, force_local=False,
                    enable_mcp=False, settings=mock.MagicMock())
    opened.assert_called_once_with("http://127.0.0.1:4321/?new=1")
