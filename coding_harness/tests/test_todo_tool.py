"""TodoWrite: strict validation, the registry's todo_update event, the mirror."""
from __future__ import annotations

import json
from unittest import mock

import pytest

from coding_harness.core.mode import Mode, is_plan_safe
from coding_harness.core.paths import meta_dir
from coding_harness.tools.registry import ToolRegistry
from coding_harness.tools.todo import TodoWrite, load_todos, summarize, validate_items

GOOD = [
    {"id": "1", "text": "read the code", "status": "done"},
    {"id": "2", "text": "edit it", "status": "in_progress"},
    {"id": "3", "text": "run tests", "status": "pending"},
]


@pytest.mark.parametrize("raw, fragment", [
    ("nope", "must be a list"),
    ([{"id": "1", "text": "x", "status": "finished"}], "status"),
    ([{"text": "x", "status": "done"}], ".id"),
    ([{"id": "", "text": "x", "status": "done"}], ".id"),
    ([{"id": "1", "text": "", "status": "done"}], ".text"),
    ([{"id": "1", "text": "x", "status": "done"}] * 2, "duplicate"),
    ([{"id": "1", "text": "x", "status": "done", "extra": 1}], "unknown keys"),
    (["just a string"], "object"),
])
def test_validation_rejects(raw, fragment):
    items, problem = validate_items(raw)
    assert items is None and fragment in problem


def test_summary_line():
    assert summarize(GOOD) == "3 items: 1 done, 1 in progress, 1 pending"


def test_run_mirrors_under_meta_dir():
    tool = TodoWrite(lambda: "todo-sess")
    result = tool.run({"items": GOOD})
    assert not result.is_error and result.content.startswith("3 items")
    path = meta_dir() / "plans" / "todo-sess.todo.json"
    assert json.loads(path.read_text())["items"] == GOOD
    assert load_todos("todo-sess") == GOOD


def test_bad_call_keeps_previous_list():
    tool = TodoWrite(lambda: "todo-keep")
    tool.run({"items": GOOD})
    result = tool.run({"items": [{"id": "1", "text": "x", "status": "??"}]})
    assert result.is_error and tool.items == GOOD


def test_plan_safe_and_visible_in_plan_mode():
    assert is_plan_safe("TodoWrite")
    reg = ToolRegistry(mode=Mode.PLAN)
    reg.register(TodoWrite(lambda: reg.session_id))
    assert [t["function"]["name"] for t in reg.to_openai_tools()] == ["TodoWrite"]


def test_dispatch_emits_todo_update():
    events = []
    reg = ToolRegistry(event_sink=events.append, session_id="todo-emit")
    reg.register(TodoWrite(lambda: reg.session_id))
    verdict = mock.MagicMock(allowed=True, reason="test", path="hook")
    with mock.patch("coding_harness.tools.registry.sentinel.review", return_value=verdict):
        result = reg.dispatch("TodoWrite", {"items": GOOD})
    assert not result.is_error
    updates = [e.payload for e in events if e.kind == "todo_update"]
    assert updates == [{"items": GOOD}]
