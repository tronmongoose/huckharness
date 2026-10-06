"""Plan-file flow: /plan runs an OFF turn and saves the plan, /act executes it."""
from __future__ import annotations

from pathlib import Path

import pytest

from coding_harness.core.mode import Autonomy, Mode, mode_for
from coding_harness.core.paths import meta_dir
from coding_harness.core.session import SessionResult
from coding_harness.core.settings import Settings
from coding_harness.modes import repl_commands
from coding_harness.security import audit


class _Registry:
    def __init__(self, autonomy=Autonomy.LOW, settings=None):
        self.autonomy = autonomy
        self.settings = settings or Settings()


class _FakeSession:
    """Records each run_turn with the autonomy level it ran at."""

    def __init__(self, registry, reply="the plan text"):
        self.registry = registry
        self.mode = mode_for(registry.autonomy)
        self.session_id = "plan-sess"
        self.agent_identity = None
        self.reply = reply
        self.turns = []  # (prompt, level, mode)

    def set_mode(self, mode, *, reason="user"):
        self.mode = mode
        return mode

    def interrupt(self):
        pass

    def run_turn(self, prompt, **_):
        self.turns.append((prompt, self.registry.autonomy, self.mode))
        return SessionResult(
            final_text=self.reply, turns=1, session_id=self.session_id,
            session_log_path=Path("/tmp/x.jsonl"), halted_reason="model_done",
        )


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_META_DIR", str(tmp_path / "meta"))
    audits = []
    monkeypatch.setattr(
        audit, "append_autonomy_change",
        lambda **kw: audits.append(kw),
    )
    return audits


def test_plan_no_arg_reports_no_plan(capsys):
    sess = _FakeSession(_Registry())
    state = repl_commands.ReplState()
    assert repl_commands.handle(sess, "/plan", state) == "handled"
    assert "no plan" in capsys.readouterr().err


def test_plan_runs_off_turn_saves_and_restores_level(capsys):
    sess = _FakeSession(_Registry(Autonomy.MEDIUM))
    state = repl_commands.ReplState()
    repl_commands.handle(sess, "/plan add a widget", state)
    (prompt, level, mode) = sess.turns[0]
    assert "add a widget" in prompt and "plan" in prompt.lower()
    assert level is Autonomy.OFF and mode is Mode.PLAN
    assert sess.registry.autonomy is Autonomy.MEDIUM  # restored
    assert sess.mode is Mode.ACT
    expected = meta_dir() / "plans" / "plan-sess.md"
    assert state.plan_path == expected
    assert expected.read_text().strip() == "the plan text"
    out = capsys.readouterr()
    assert str(expected) in out.err
    assert "the plan text" in out.out


def test_plan_then_no_arg_prints_the_path(capsys):
    sess = _FakeSession(_Registry())
    state = repl_commands.ReplState()
    repl_commands.handle(sess, "/plan goal", state)
    repl_commands.handle(sess, "/plan", state)
    assert str(state.plan_path) in capsys.readouterr().err


def test_act_without_plan_says_so(capsys):
    sess = _FakeSession(_Registry())
    state = repl_commands.ReplState()
    repl_commands.handle(sess, "/act", state)
    assert "no plan" in capsys.readouterr().err
    assert sess.turns == []


def test_act_reads_plan_and_raises_off_to_low(tmp_path):
    plan = tmp_path / "p.md"
    plan.write_text("step one\nstep two\n")
    sess = _FakeSession(_Registry(Autonomy.OFF))
    state = repl_commands.ReplState()
    repl_commands.handle(sess, f"/act {plan}", state)
    (prompt, level, mode) = sess.turns[0]
    assert "step one" in prompt and "Execute this plan" in prompt
    assert level is Autonomy.LOW and mode is Mode.ACT
    assert state.plan_path == plan


def test_act_uses_saved_plan_from_state(capsys):
    sess = _FakeSession(_Registry(Autonomy.OFF))
    state = repl_commands.ReplState()
    repl_commands.handle(sess, "/plan do the thing", state)
    repl_commands.handle(sess, "/act", state)
    prompt, level, _ = sess.turns[1]
    assert "the plan text" in prompt
    assert level is Autonomy.LOW


def test_act_honors_higher_settings_default():
    sess = _FakeSession(_Registry(
        Autonomy.OFF, settings=Settings(autonomy=Autonomy.MEDIUM),
    ))
    state = repl_commands.ReplState()
    repl_commands.handle(sess, "/plan g", state)
    repl_commands.handle(sess, "/act", state)
    assert sess.turns[1][1] is Autonomy.MEDIUM


def test_act_never_lowers_a_higher_level():
    sess = _FakeSession(_Registry(Autonomy.HIGH))
    state = repl_commands.ReplState()
    repl_commands.handle(sess, "/plan g", state)
    repl_commands.handle(sess, "/act", state)
    assert sess.turns[1][1] is Autonomy.HIGH


def test_act_missing_file_reports_error(tmp_path, capsys):
    sess = _FakeSession(_Registry())
    state = repl_commands.ReplState()
    repl_commands.handle(sess, f"/act {tmp_path / 'absent.md'}", state)
    assert "cannot read plan" in capsys.readouterr().err
    assert sess.turns == []
