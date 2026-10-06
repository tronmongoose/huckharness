"""Explore: a read-only research subagent on the small explore-role model.

The coding agent hands it a question about the repository; a child session
with only Read, Grep and Glob, in Plan mode, answers it and returns a report.
The big coder keeps its context for the change while the small model does the
searching. The child has no Bash, no Brain and no Explore of its own, runs on
local Ollama only, and is bounded by a step deadline.

Its reads are confined to the project by the same envelope a serve session
gets at autonomy off. A small model asked a question will happily grep the
whole disk; unconfined, that reaches the user's vaults and hands their
contents up to a parent whose next turn may route to a frontier model.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from .base import Tool, ToolResult

DEADLINE_S = 240.0
REPORT_MAX = 6000
SUMMARY_MAX = 120
_SYSTEM = (
    "You are a read-only research assistant inside a coding agent. The repository is "
    "{root}. Answer the task using Read, Grep and Glob on paths inside it; prefer "
    "relative paths and short regex patterns of one or two words. Anything outside "
    "the repository is refused. Do not propose or describe edits. Finish with a "
    "concise report: the facts found, each with its file path and line numbers, and "
    "say plainly what you could not find."
)


class Explore(Tool):
    name = "Explore"
    category = "read"
    description = (
        "Send a read-only research helper to search and read the repository and report "
        "back. Use it for broad questions (where is X defined, how does Y flow, which "
        "files touch Z) so your own context stays on the change. It cannot edit or run "
        "commands."
    )
    parameters = {
        "type": "object",
        "properties": {
            "task": {"type": "string",
                     "description": "The question to answer, with any paths or names you know."},
        },
        "required": ["task"],
    }

    def __init__(self, settings: Any = None):
        self.settings = settings
        # Set by the session before each step; carries the child's progress to
        # the GUI. Only tool names and short summaries cross, never content.
        self.emit: Callable[[str, dict[str, Any]], None] | None = None

    def _forward(self, child_id: list[str]) -> Callable[[Any], None]:
        """A child event sink that reports each tool start to the parent as a subagent_step."""
        def sink(event: Any) -> None:
            if self.emit is None or event.kind != "tool_call_start":
                return
            args = event.payload.get("args") or {}
            hint = next((str(args[k]) for k in ("file_path", "pattern", "path") if args.get(k)), "")
            root = os.path.realpath(os.getcwd()) + os.sep
            real = os.path.realpath(hint) if os.path.isabs(hint) else hint
            hint = real[len(root):] if real.startswith(root) else hint
            self.emit("subagent_step", {"id": child_id[0], "tool": str(event.payload.get("tool", "?")),
                                        "summary": hint[:SUMMARY_MAX]})
        return sink

    def _child(self, sink: Callable[[Any], None] | None = None) -> Any:
        """A fresh Plan-mode session with the read-only tools and the explore model."""
        import os

        from coding_harness.core.envelope import preset
        from coding_harness.core.mode import Autonomy, Mode
        from coding_harness.core.model_roles import role_model
        from coding_harness.core.session import Session
        from coding_harness.tools.glob_tool import Glob
        from coding_harness.tools.grep import Grep
        from coding_harness.tools.read import Read
        from coding_harness.tools.registry import ToolRegistry

        registry = ToolRegistry(event_sink=None)
        for tool in (Read(), Grep(), Glob()):
            registry.register(tool)
        registry.autonomy = Autonomy.OFF
        registry.settings = self.settings
        root = os.path.realpath(os.getcwd())
        return Session(
            model=role_model("explore"), registry=registry,
            system_prompt=_SYSTEM.format(root=root),
            event_sink=sink, force_local=True, explicit_model=True, mode=Mode.PLAN,
            subagent="explore",
            envelope=preset(root, Autonomy.OFF, self.settings),
            verify_repair=False, agentic_review=False,
            done_gate_enabled=False, targeted_tests_enabled=False,
        )

    def run(self, args: dict[str, Any]) -> ToolResult:
        """Run the child to completion (or its deadline) and return its report."""
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            return ToolResult("task must be a non-empty string", is_error=True)
        child_id = [""]
        child = self._child(self._forward(child_id))
        child_id[0] = child.session_id
        if self.emit is not None:
            self.emit("subagent_start", {"id": child.session_id, "agent": "explore",
                                         "task": task[:SUMMARY_MAX], "model": child.model})
        halted, steps = "error", 0
        try:
            result = child.run_turn(task, deadline_s=DEADLINE_S)
            halted, steps = result.halted_reason, result.turns
        finally:
            child.close()
            if self.emit is not None:
                self.emit("subagent_done", {"id": child.session_id, "steps": steps, "halted": halted})
        report = (result.final_text or "").strip() or "(the helper returned no report)"
        if len(report) > REPORT_MAX:
            report = report[:REPORT_MAX] + "\n[report truncated]"
        head = f"Explore report ({child.model}, {result.turns} steps, {result.halted_reason}):"
        return ToolResult(f"{head}\n\n{report}", is_error=result.halted_reason == "error",
                          metadata={"model": child.model, "steps": result.turns,
                                    "halted": result.halted_reason,
                                    "child_session": child.session_id})
