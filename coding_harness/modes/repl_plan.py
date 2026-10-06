"""Plan-file flow for the REPL: /plan writes a plan, /act executes it.

``/plan <goal>`` drops autonomy to OFF for one turn (read-only investigation),
asks the model for a concrete markdown plan, and saves the final text to
``meta_dir()/plans/<session_id>.md``. ``/act [path]`` re-reads that plan,
raises autonomy to at least LOW (or the settings default if higher) and runs
an execution turn with the plan text as context. Serve reuses the prompts,
the path and the level order for its /plan and /act routes.

Exports: PLAN_INSTRUCTION, ACT_INSTRUCTION, plan_path_for(session_id), cmd_plan, cmd_act.
"""
from __future__ import annotations

import sys
from pathlib import Path

from coding_harness.core.mode import Autonomy, set_autonomy
from coding_harness.core.paths import meta_dir
from coding_harness.modes.interruptible import run_interruptible

PLAN_INSTRUCTION = (
    "Write a concrete implementation plan for the goal below, as markdown. "
    "Investigate with your read-only tools first. The plan must name the "
    "files to change, the ordered steps, and how to verify the result. "
    "Do not make any changes.\n\nGoal: "
)
ACT_INSTRUCTION = (
    "Execute this plan. Follow its steps, then run its verification.\n\n"
)
_LEVEL_ORDER = [Autonomy.OFF, Autonomy.LOW, Autonomy.MEDIUM, Autonomy.HIGH]


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def plan_path_for(session_id: str) -> Path:
    """Where the plan for ``session_id`` lives: ``meta_dir()/plans/<id>.md``."""
    return meta_dir() / "plans" / f"{session_id}.md"


def _run_and_print(session, prompt: str, state) -> object:
    """One turn through the interruptible runner, final text to stdout."""
    result = run_interruptible(session, lambda: session.run_turn(prompt))
    state.record(result)
    if result.final_text:
        print(result.final_text, flush=True)
    return result


def cmd_plan(session, line: str, state) -> None:
    """/plan [goal]: no arg shows the plan path; a goal runs a planning turn."""
    parts = line.split(None, 1)
    if len(parts) == 1:
        if state.plan_path is None:
            _say("  no plan")
        else:
            _say(f"  plan {state.plan_path}")
        return
    goal = parts[1].strip()
    previous = session.registry.autonomy
    set_autonomy(session, Autonomy.OFF, "repl_plan")
    try:
        result = _run_and_print(session, PLAN_INSTRUCTION + goal, state)
    finally:
        set_autonomy(session, previous, "repl_plan_restore")
    if not result.final_text:
        _say("  /plan: the turn produced no plan text")
        return
    path = plan_path_for(session.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.final_text + "\n", encoding="utf-8")
    state.plan_path = path
    _say(f"  plan saved: {path}")


def cmd_act(session, line: str, state) -> None:
    """/act [path]: execute the saved plan (or the one at ``path``)."""
    parts = line.split(None, 1)
    path = Path(parts[1].strip()) if len(parts) > 1 else state.plan_path
    if path is None:
        _say("  /act: no plan; run /plan <goal> first")
        return
    try:
        plan_text = path.read_text(encoding="utf-8")
    except OSError as e:
        _say(f"  /act: cannot read plan: {e}")
        return
    state.plan_path = path
    floor = Autonomy.LOW
    settings = getattr(session.registry, "settings", None)
    default = getattr(settings, "autonomy", None) or Autonomy.LOW
    if _LEVEL_ORDER.index(default) > _LEVEL_ORDER.index(floor):
        floor = default
    current = session.registry.autonomy
    if _LEVEL_ORDER.index(current) < _LEVEL_ORDER.index(floor):
        set_autonomy(session, floor, "repl_act")
    _run_and_print(session, ACT_INSTRUCTION + plan_text, state)
