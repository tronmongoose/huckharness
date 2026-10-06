"""Serve and the GUI attach the settings-declared hook runner."""
from __future__ import annotations

from types import SimpleNamespace

from coding_harness.core.hooks import HookRunner
from coding_harness.core.mode import Mode
from coding_harness.core.settings import Settings
from coding_harness.modes import serve_mode

_DENY_RM = {"PreToolUse": [{"matcher": "Bash(rm *)", "command": "exit 2"}]}


def _state(settings: Settings) -> serve_mode._ServerState:
    return serve_mode._ServerState(
        model="stub", force_local=True, explicit_model=True,
        enable_mcp=False, settings=settings,
    )


def test_session_gets_a_hook_runner_on_its_registry():
    entry = _state(Settings(hooks=_DENY_RM)).create_session(mode=Mode.ACT)
    assert isinstance(entry.hooks, HookRunner)
    assert entry.session.registry.hooks is entry.hooks


def test_no_hooks_configured_leaves_the_registry_untouched():
    entry = _state(Settings()).create_session(mode=Mode.ACT)
    assert entry.hooks is None
    assert entry.session.registry.hooks is None


def test_kill_switch_disables_serve_hooks(monkeypatch):
    monkeypatch.setenv("HARNESS_HOOKS", "0")
    entry = _state(Settings(hooks=_DENY_RM)).create_session(mode=Mode.ACT)
    assert entry.hooks is None


class _FakeHooks:
    def __init__(self, stop_messages):
        self.events = []
        self._stop = stop_messages

    def run(self, event, **_kw):
        self.events.append(event)
        msgs = self._stop if event == "Stop" else []
        return SimpleNamespace(stop_messages=list(msgs))


class _FakeSession:
    def __init__(self):
        self.prompts = []

    def run_turn(self, text, **_kw):
        self.prompts.append(text)
        return f"result:{text}"


def test_hooked_turn_fires_prompt_and_stop_around_the_turn():
    hooks, session = _FakeHooks([]), _FakeSession()
    entry = SimpleNamespace(hooks=hooks, session=session)
    assert serve_mode._hooked_turn(entry, "do it", None, None) == "result:do it"
    assert hooks.events == ["UserPromptSubmit", "Stop"]
    assert session.prompts == ["do it"]


def test_stop_block_feeds_back_exactly_one_extra_turn():
    hooks, session = _FakeHooks(["tests are red"]), _FakeSession()
    entry = SimpleNamespace(hooks=hooks, session=session)
    assert serve_mode._hooked_turn(entry, "do it", None, None) == "result:tests are red"
    assert session.prompts == ["do it", "tests are red"]


def test_no_hooks_is_a_plain_turn():
    session = _FakeSession()
    entry = SimpleNamespace(hooks=None, session=session)
    assert serve_mode._hooked_turn(entry, "do it", None, None) == "result:do it"
