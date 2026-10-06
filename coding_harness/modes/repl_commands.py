"""Slash commands for the REPL.

Exports ``HELP_TEXT``, ``ReplState`` and ``handle``. ``handle`` returns
``"exit"`` to end the session, ``"handled"`` when a command ran, or ``None``
when the line is a prompt for the model.
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from coding_harness.core.mode import Mode, parse_autonomy, set_autonomy
from coding_harness.core.session import Session, SessionResult
from coding_harness.models.ollama import BannedModelError, assert_model_allowed
from coding_harness.models.profile import resolve_profile
from coding_harness.modes.repl_plan import cmd_act, cmd_plan

HELP_TEXT = """\
bjorn REPL

Type a prompt and press Enter. The agent will think, call tools, and reply.
A line ending in a backslash continues on the next line; a line that is
exactly ``` starts a block that ends at the next ```.

Slash commands:
  /exit, /quit       end the session
  /autonomy [level]  show or set the autonomy level: off, low, medium, high
                     off = read-only, low = edits + tests (default),
                     medium = commits, installs, file ops, high = network
                     and anything the blocklist leaves alone. Commands the
                     level does not cover pause for a y/N approval.
  /mode [plan|act]   switch tool surface; no arg toggles
                     plan = read-only (Read/Grep/Glob + MCP reads)
                     act = full surface
  /plan [goal]       run a read-only planning turn for the goal and save the
                     plan to meta_dir()/plans/<session_id>.md; no arg shows
                     the current plan path
  /act [path]        execute the saved plan (or the plan file at path) at
                     autonomy LOW, or the settings default if higher
  /model [tag]       show the model, or switch to another Ollama tag
  /rewind [n]        undo the last n user-prompt turns (default 1):
                     restores any files written, drops messages
  /compact           fold older history into a local-model summary now
  /context           context fill: last prompt tokens vs the model's num_ctx
  /diff              git diff of the files this session changed
  /skill <name>      inject a skill's SKILL.md as the next prompt
  /<name> [args]     run a custom command from .bjorn/commands/<name>.md
  /help              show this help
  EOF (Ctrl-D)       end the session
  Ctrl-C             interrupt the running turn; at the prompt, exit

Every Write/Edit pauses for a unified-diff confirmation (y/N/d=full diff).
Every tool call is gated by Sentinel and audited to a hash chain.
Session history persists for the lifetime of the REPL.
"""


@dataclass
class ReplState:
    """What the commands need from earlier turns."""

    last_result: SessionResult | None = None
    files_changed: set[str] = field(default_factory=set)
    plan_path: Path | None = None

    def record(self, result: SessionResult) -> None:
        self.last_result = result
        self.files_changed.update(result.files_changed)


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def handle(session: Session, line: str, state: ReplState) -> str | None:
    """Run the slash command on ``line``; None when it is not one."""
    if line in ("/exit", "/quit"):
        return "exit"
    if line == "/help":
        _say(HELP_TEXT)
        return "handled"
    word = line.split(None, 1)[0]
    fn = _COMMANDS.get(word)
    if fn is None:
        return None
    fn(session, line, state)
    return "handled"


def _autonomy(session: Session, line: str, _: ReplState) -> None:
    """/autonomy [level]: show the level, or change it and audit the change."""
    parts = line.split()
    registry = session.registry
    if len(parts) == 1:
        _say(f"  autonomy {registry.autonomy.value} · mode {session.mode.value}")
        return
    try:
        level = parse_autonomy(parts[1])
    except ValueError as e:
        _say(f"  /autonomy: {e}")
        return
    set_autonomy(session, level, "repl_command")
    _say(f"  autonomy → {level.value} · mode {session.mode.value}")


def _mode(session: Session, line: str, _: ReplState) -> None:
    """/mode [plan|act]: switch the tool surface; no arg toggles."""
    parts = line.split()
    if len(parts) == 1:
        target = Mode.PLAN if session.mode is Mode.ACT else Mode.ACT
    else:
        arg = parts[1].lower()
        if arg not in ("plan", "act"):
            _say("  /mode: usage /mode [plan|act] (no arg toggles)")
            return
        target = Mode(arg)
    new_mode = session.set_mode(target, reason="repl_command")
    visible = sorted(
        t for t in session.registry.tools if session.registry._visible(t)
    )
    _say(f"  mode → {new_mode.value} · tools: {', '.join(visible)}")


def _rewind(session: Session, line: str, _: ReplState) -> None:
    """/rewind [n]: undo the last n prompt turns and their file writes."""
    parts = line.split()
    try:
        n = int(parts[1]) if len(parts) > 1 else 1
        if n < 1:
            raise ValueError("n must be >= 1")
    except ValueError as e:
        _say(f"  /rewind: {e}")
        return
    report = session.rewind(n)
    msg = (
        f"  rewound {report.turns_rewound} turn(s) · "
        f"restored {report.files_restored} file(s) · "
        f"deleted {report.files_deleted} file(s)"
    )
    if report.errors:
        msg += f" · {len(report.errors)} error(s)"
    _say(msg)
    for err in report.errors:
        _say(f"    error: {err}")


def _model(session: Session, line: str, _: ReplState) -> None:
    """/model [tag]: show the model, or switch every later turn to ``tag``."""
    parts = line.split()
    if len(parts) == 1:
        _say(f"  model {session.model}")
        return
    tag = parts[1]
    try:
        assert_model_allowed(tag)
    except BannedModelError as e:
        _say(f"  /model: {e}")
        return
    session.model = tag
    session.profile = resolve_profile(tag)
    session.explicit_model = True
    _say(f"  model → {tag}")


def _compact(session: Session, _line: str, _: ReplState) -> None:
    """/compact: summarize older history now, whatever its size."""
    result = session.compact()
    if not result.compacted:
        _say(f"  compaction skipped: {result.reason}")
        return
    _say(
        f"  compacted {result.messages_before} → {result.messages_after} messages · "
        f"{result.chars_before} → {result.chars_after} chars"
    )


def _context(session: Session, _line: str, state: ReplState) -> None:
    """/context: the last model read's prompt tokens against num_ctx."""
    if state.last_result is None:
        _say("  context: no turn yet")
        return
    num_ctx = session.profile.num_ctx
    used = session.last_prompt_tokens
    pct = (100 * used / num_ctx) if num_ctx else 0.0
    _say(
        f"  context {used} / {num_ctx} tokens ({pct:.0f}%) · "
        f"last turn {state.last_result.tokens_in} tokens_in over "
        f"{state.last_result.turns} step(s)"
    )


def _diff(_session: Session, _line: str, state: ReplState) -> None:
    """/diff: git diff of every file this session's turns changed."""
    if not state.files_changed:
        _say("  no files changed this session")
        return
    proc = subprocess.run(
        ["git", "diff", "--", *sorted(state.files_changed)],
        cwd=os.getcwd(), capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        _say(f"  /diff: git exited {proc.returncode}: {proc.stderr.strip()}")
        return
    print(proc.stdout, end="", flush=True)
    if not proc.stdout:
        _say("  no unstaged changes in the files this session touched")


_COMMANDS = {
    "/autonomy": _autonomy,
    "/mode": _mode,
    "/plan": cmd_plan,
    "/act": cmd_act,
    "/rewind": _rewind,
    "/model": _model,
    "/compact": _compact,
    "/context": _context,
    "/diff": _diff,
}
