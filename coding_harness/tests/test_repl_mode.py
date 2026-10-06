"""Multi-turn REPL semantics: Session.run_turn carries history across turns.

We don't actually run the Ollama process — that's slow and non-deterministic.
We patch ``ollama.chat`` to return canned assistant messages and assert that:

  - The system prompt is seeded once, on the first turn.
  - ``session_start`` fires exactly once, on the first turn.
  - Each ``run_turn`` appends a ``user`` message and (since the canned reply
    has no tool calls) one ``assistant`` message.
  - Subsequent turns receive the cumulative history, so the model can answer
    follow-ups that depend on prior context.
  - ``session_id`` is stable across turns and the session log JSONL contains
    both turns.
  - The audit chain is unaffected (no tool calls in this scenario; the chain
    head doesn't move) and still verifies.
  - ``close()`` is idempotent and emits ``session_done`` exactly once.

The second half drives ``repl_mode.run`` itself: Ctrl-C mid-turn, the slash
commands against a fake Session, and ``--resume`` against a transcript.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from coding_harness.core import session as session_mod
from coding_harness.core.session import SessionResult
from coding_harness.models import ollama as ollama_mod
from coding_harness.modes import repl_mode
from coding_harness.security import audit
from coding_harness.tools.registry import ToolRegistry


class ReplMultiTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        # Redirect every on-disk side effect into the temp dir so nothing the
        # harness writes leaks into ~/slos.
        self._patches = [
            patch.object(audit, "META_DIR", self.tmp_path),
            patch.object(audit, "AUDIT_PATH", self.tmp_path / "audit.jsonl"),
            patch.object(audit, "ANCHORS_PATH", self.tmp_path / "anchors.jsonl"),
            patch.object(session_mod, "SESSIONS_DIR", self.tmp_path / "sessions"),
        ]
        for p in self._patches:
            p.start()

        self.events: list[tuple[str, dict]] = []

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def _sink(self, ev) -> None:  # type: ignore[no-untyped-def]
        self.events.append((ev.kind, ev.payload))

    def _make_session(self) -> session_mod.Session:
        registry = ToolRegistry(event_sink=self._sink)
        return session_mod.Session(
            model="mistral-small3.2:latest",
            registry=registry,
            system_prompt="SYSPROMPT",
            event_sink=self._sink,
        )

    def test_two_turns_share_history_and_session_id(self) -> None:
        replies = iter([
            {"role": "assistant", "content": "first reply", "tool_calls": []},
            {"role": "assistant", "content": "second reply", "tool_calls": []},
        ])

        def fake_chat(*, model, messages, tools=None, **_):  # type: ignore[no-untyped-def]
            return next(replies)

        with patch.object(ollama_mod, "chat", side_effect=fake_chat):
            sess = self._make_session()
            r1 = sess.run_turn("hello")
            r2 = sess.run_turn("and then?")

        self.assertEqual(r1.halted_reason, "model_done")
        self.assertEqual(r1.final_text, "first reply")
        self.assertEqual(r2.halted_reason, "model_done")
        self.assertEqual(r2.final_text, "second reply")
        self.assertEqual(r1.session_id, r2.session_id)

        # History after two turns:
        # [system, user1, assistant1, user2, assistant2]
        msgs = sess.messages
        self.assertEqual(len(msgs), 5)
        self.assertEqual(msgs[0]["role"], "system")
        self.assertEqual(msgs[0]["content"], "SYSPROMPT")
        self.assertEqual(msgs[1], {"role": "user", "content": "hello"})
        self.assertEqual(msgs[2]["role"], "assistant")
        self.assertEqual(msgs[2]["content"], "first reply")
        self.assertEqual(msgs[3], {"role": "user", "content": "and then?"})
        self.assertEqual(msgs[4]["role"], "assistant")
        self.assertEqual(msgs[4]["content"], "second reply")

    def test_session_start_fires_once_session_done_on_close(self) -> None:
        replies = iter([
            {"role": "assistant", "content": "ok", "tool_calls": []},
            {"role": "assistant", "content": "ok again", "tool_calls": []},
        ])
        with patch.object(ollama_mod, "chat", side_effect=lambda **kw: next(replies)):
            sess = self._make_session()
            sess.run_turn("turn1")
            sess.run_turn("turn2")
            sess.close()
            sess.close()  # idempotent

        kinds = [k for (k, _) in self.events]
        self.assertEqual(kinds.count("session_start"), 1)
        self.assertEqual(kinds.count("session_done"), 1)

    def test_session_log_contains_both_user_messages(self) -> None:
        replies = iter([
            {"role": "assistant", "content": "a", "tool_calls": []},
            {"role": "assistant", "content": "b", "tool_calls": []},
        ])
        with patch.object(ollama_mod, "chat", side_effect=lambda **kw: next(replies)):
            sess = self._make_session()
            sess.run_turn("first")
            sess.run_turn("second")
            sess.close()

        log_path = sess.session_log_path
        self.assertTrue(log_path.exists())
        records = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        kinds = [r["kind"] for r in records]
        self.assertEqual(kinds.count("user_message"), 2)
        user_contents = [r["content"] for r in records if r["kind"] == "user_message"]
        self.assertEqual(user_contents, ["first", "second"])
        self.assertEqual(kinds.count("session_start"), 1)
        self.assertEqual(kinds.count("session_done"), 1)

    def test_audit_chain_still_verifies_after_repl(self) -> None:
        # No tool calls fire in this test, so the audit chain stays empty,
        # but verify_chain() must still pass on an empty chain.
        replies = iter([
            {"role": "assistant", "content": "x", "tool_calls": []},
        ])
        with patch.object(ollama_mod, "chat", side_effect=lambda **kw: next(replies)):
            sess = self._make_session()
            sess.run_turn("noop")
            sess.close()

        ok, msg = audit.verify_chain()
        self.assertTrue(ok, msg)

    def test_run_turn_after_close_raises(self) -> None:
        replies = iter([
            {"role": "assistant", "content": "x", "tool_calls": []},
        ])
        with patch.object(ollama_mod, "chat", side_effect=lambda **kw: next(replies)):
            sess = self._make_session()
            sess.run_turn("hi")
            sess.close()
            with self.assertRaises(RuntimeError):
                sess.run_turn("after close")

    def test_oneshot_run_still_emits_session_done(self) -> None:
        # Backward-compat: Session.run() is the one-shot entry point used by
        # print mode. It must still emit session_start + session_done in one
        # call, even after the multi-turn refactor.
        replies = iter([
            {"role": "assistant", "content": "done", "tool_calls": []},
        ])
        with patch.object(ollama_mod, "chat", side_effect=lambda **kw: next(replies)):
            sess = self._make_session()
            result = sess.run("just one prompt")

        self.assertEqual(result.halted_reason, "model_done")
        kinds = [k for (k, _) in self.events]
        self.assertEqual(kinds.count("session_start"), 1)
        self.assertEqual(kinds.count("session_done"), 1)


# ── REPL loop: interrupt, slash commands, resume ─────────────────────


def _line_reader(lines):
    it = iter(lines)

    def read(_prompt=""):
        value = next(it)
        if isinstance(value, BaseException):
            raise value
        return value
    return read


def _write_transcript(sessions_dir: Path, session_id: str, cwd: str) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    records = [
        {"kind": "session_start", "session_id": session_id, "model": "m", "mode": "act",
         "cwd": cwd},
        {"kind": "user_message", "content": "remember 42"},
        {"kind": "assistant_message", "content": "noted", "tool_calls": []},
    ]
    (sessions_dir / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8",
    )


@dataclass
class _CompactReport:
    compacted: bool = True
    reason: str = ""
    messages_before: int = 9
    messages_after: int = 4
    chars_before: int = 5000
    chars_after: int = 800


class _FakeSession:
    """Canned turns; ``run_turn`` can block until interrupted."""
    wait_for_interrupt = False

    def __init__(self, **kw):
        self.model = kw.get("model", "m")
        self.registry = kw["registry"]
        self.system_prompt = kw.get("system_prompt", "")
        self.mode = kw.get("mode")
        self.session_id = kw.get("session_id", "fake")
        self.session_log_path = Path("/tmp/fake.jsonl")
        self.agent_identity = None
        self.total_turns = 0
        self.last_prompt_tokens = 0
        self.explicit_model = False
        self.profile = type("P", (), {"num_ctx": 1000})()
        self.interrupted = False
        self._stop = threading.Event()

    def interrupt(self):
        self.interrupted = True
        self._stop.set()

    def run_turn(self, prompt, **_):
        import _thread
        self.total_turns += 1
        self.last_prompt_tokens = 250
        if self.wait_for_interrupt:
            threading.Timer(0.2, _thread.interrupt_main).start()
            self._stop.wait(5)
            reason = "interrupted"
        else:
            reason = "model_done"
        return SessionResult(final_text=f"reply to {prompt}", turns=1, session_id="fake",
                             session_log_path=self.session_log_path, halted_reason=reason,
                             tokens_in=900, files_changed=["a.py"])

    def compact(self):
        return _CompactReport()

    def close(self):
        pass


def _drive(monkeypatch, capsys, tmp_path, lines, **attrs):
    for k, v in attrs.items():
        monkeypatch.setattr(_FakeSession, k, v)
    monkeypatch.setattr(repl_mode, "Session", _FakeSession)
    monkeypatch.setenv("HARNESS_TOOL_PROBE", "0")
    monkeypatch.setenv("HARNESS_REPO_MAP", "0")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("builtins.input", _line_reader(lines))
    rc = repl_mode.run(enable_mcp=False, force_local=True, model="m")
    out, err = capsys.readouterr()
    return rc, out, err


def test_ctrl_c_mid_turn_interrupts_the_turn_and_keeps_the_repl(monkeypatch, capsys, tmp_path):
    rc, out, err = _drive(monkeypatch, capsys, tmp_path, ["hello", EOFError()],
                          wait_for_interrupt=True)
    assert rc == 0
    assert "· interrupted" in err and "[coding_harness] interrupted" not in err
    assert "reply to hello" in out


def test_ctrl_c_at_the_prompt_exits_130(monkeypatch, capsys, tmp_path):
    rc, _, err = _drive(monkeypatch, capsys, tmp_path, [KeyboardInterrupt()])
    assert rc == 130 and "[coding_harness] interrupted" in err


def test_answers_go_to_stdout_and_the_glyph_to_stderr(monkeypatch, capsys, tmp_path):
    rc, out, err = _drive(monkeypatch, capsys, tmp_path, ["hi", EOFError()])
    assert rc == 0 and out.strip() == "reply to hi"
    assert "❯" in err and "❯" not in out


def test_model_context_compact_and_diff_commands(monkeypatch, capsys, tmp_path):
    lines = ["/model", "/model qwen2.5:7b", "/model gpt-oss:20b", "/context", "/diff",
             "hi", "/context", "/compact", "/diff", EOFError()]
    called = {}

    def fake_run(cmd, **kw):
        called["cmd"], called["cwd"] = cmd, kw["cwd"]
        return type("P", (), {"returncode": 0, "stdout": "diff --git a/a.py\n", "stderr": ""})()

    monkeypatch.setattr("coding_harness.modes.repl_commands.subprocess.run", fake_run)
    rc, out, err = _drive(monkeypatch, capsys, tmp_path, lines)
    assert rc == 0
    assert "  model m" in err and "/model: refusing" in err and "model → gpt-oss:20b" in err
    assert "context: no turn yet" in err
    assert "no files changed this session" in err
    # /model swapped the fake profile for gpt-oss's real one (num_ctx 32768).
    assert "context 250 / 32768 tokens (1%) · last turn 900 tokens_in" in err
    assert "compacted 9 → 4 messages · 5000 → 800 chars" in err
    assert called["cmd"][:3] == ["git", "diff", "--"] and called["cmd"][3].endswith("a.py")
    assert called["cwd"] == str(tmp_path) and "diff --git a/a.py" in out


def test_multi_line_prompt_reaches_the_session(monkeypatch, capsys, tmp_path):
    seen = {}
    original = _FakeSession.run_turn

    def spy(self, prompt, **kw):
        seen["prompt"] = prompt
        return original(self, prompt, **kw)

    rc, _, _ = _drive(monkeypatch, capsys, tmp_path,
                      ["```", "line one", "line two", "```", EOFError()], run_turn=spy)
    assert rc == 0 and seen["prompt"] == "line one\nline two"


def test_find_transcript_by_id_and_newest_for_cwd(tmp_path, monkeypatch):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", sessions)
    _write_transcript(sessions, "old-here", "/work/here")
    _write_transcript(sessions, "new-there", "/work/there")
    os.utime(sessions / "old-here.jsonl", (1, 1))
    assert repl_mode.find_transcript("old-here", "/x") == sessions / "old-here.jsonl"
    assert repl_mode.find_transcript("missing", "/x") is None
    assert repl_mode.find_transcript(None, "/work/here") == sessions / "old-here.jsonl"
    assert repl_mode.find_transcript(None, "/work/there") == sessions / "new-there.jsonl"
    assert repl_mode.find_transcript(None, "/nowhere") is None


class ReplResumeTests(ReplMultiTurnTests):
    """--resume replays the transcript into the live REPL session."""

    def test_resume_replays_history_and_reports_turns(self) -> None:
        sessions = self.tmp_path / "sessions"
        _write_transcript(sessions, "s-prior", str(self.tmp_path))
        seen: list[list[dict]] = []

        def fake_chat(*, model, messages, tools=None, **_):  # type: ignore[no-untyped-def]
            seen.append(list(messages))
            return {"role": "assistant", "content": "43", "tool_calls": []}

        err = io.StringIO()
        with patch.object(ollama_mod, "chat", side_effect=fake_chat), \
                patch("builtins.input", _line_reader(["and now?", EOFError()])), \
                patch.dict(os.environ, {"HARNESS_TOOL_PROBE": "0", "HARNESS_REPO_MAP": "0"}), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = repl_mode.run(enable_mcp=False, force_local=True, resume="s-prior")
        self.assertEqual(rc, 0)
        self.assertIn("resumed session s-prior · 1 turn(s)", err.getvalue())
        contents = [m.get("content") for m in seen[0]]
        self.assertIn("remember 42", contents)
        self.assertIn("and now?", contents)
        self.assertEqual(seen[0][0]["role"], "system")
        records = [json.loads(ln) for ln in (sessions / "s-prior.jsonl").read_text().splitlines()]
        self.assertEqual([r["kind"] for r in records].count("session_start"), 1)
        self.assertEqual([r["content"] for r in records if r["kind"] == "user_message"],
                         ["remember 42", "and now?"])

    def test_resume_unknown_session_exits_2(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = repl_mode.run(enable_mcp=False, force_local=True, resume="nope")
        self.assertEqual(rc, 2)
        self.assertIn("no transcript for session nope", err.getvalue())


if __name__ == "__main__":
    unittest.main()
