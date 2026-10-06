"""Tool ABC + result type for the coding harness.

A Tool is a callable with a JSON-Schema-described argument shape. The schema is
sent to the model as part of the OpenAI-compatible ``tools`` parameter; the
model then emits ``tool_calls`` whose arguments must validate against it.

Schemas are written by hand for M1 (two tools, simple shapes). M2 may switch to
generating them from typed dataclasses if it pays for itself.

Two tool flavours:

  * ``Tool`` — the base contract. One method, ``run(args)``, returns a result.
  * ``PlannedTool`` — write-class tools that expose a pre-flight ``plan(args)``
    producing a ``WritePlan`` (with the unified diff), then ``apply(plan)`` to
    commit. The registry uses this split to (a) show the operator the diff
    before any disk write and (b) capture the pre-image for per-turn snapshots.
    ``run()`` is provided as a back-compat helper that just chains plan+apply,
    so existing direct-call sites (and tests) keep working.
"""
from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def diff_label(path: Path | str) -> str:
    """``/rel/path`` under the working directory for ``a{label}``/``b{label}`` headers, else the path as given.

    A diff header is shown to the operator and can ride a transcript; the
    absolute home layout adds nothing there.
    """
    text = str(path)
    try:
        rel = os.path.relpath(text, os.getcwd())
    except ValueError:
        return text
    if rel == os.curdir or rel.startswith(os.pardir):
        return text
    return "/" + rel


@dataclass
class ToolResult:
    """The result of one tool dispatch.

    ``content`` is what gets fed back to the model as the ``tool`` role message.
    Keep it human-readable text — for binary or huge output, summarize and
    point at a path on disk."""

    content: str
    is_error: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class WritePlan:
    """A pre-flight description of a write-class tool's intended mutation.

    Holds everything the registry needs to (a) ask the operator for confirmation
    with a unified diff and (b) capture a snapshot of the pre-image for rewind.
    No disk write happens until the tool's ``apply(plan)`` is called.
    """

    tool: str  # "Write" or "Edit"
    file_path: Path
    existed: bool
    pre_image: bytes | None  # None when the file didn't exist
    post_image: bytes
    unified_diff: str  # plain text, no ANSI; renderer adds color
    summary: str  # one-line "Edit foo.py (1 replacement)"
    metadata: dict[str, Any] = field(default_factory=dict)


class Tool(ABC):
    """Subclass and set ``name``, ``description``, ``parameters`` and
    ``category`` ("read" | "edit" | "execute"). The category drives the
    in-process Sentinel policy and the envelope's category fallback, so a
    tool that mutates state must not leave the "read" default in place."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}
    category: str = "read"

    @abstractmethod
    def run(self, args: dict[str, Any]) -> ToolResult:
        """Execute the tool. Args have already passed JSON-Schema-shaped checks
        from the model side, but you should still validate types defensively
        because the model lies sometimes."""
        ...

    # Wire format expected by Ollama / OpenAI tool-calling.
    def to_openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class PlannedTool(Tool):
    """A tool that supports plan() + apply() in addition to run().

    The registry detects ``isinstance(tool, PlannedTool)`` and routes the
    dispatch through plan → confirm → snapshot → apply. Direct callers (tests,
    one-shot scripts) can still use ``run(args)`` and get the legacy
    "validate → mutate → return" behavior in one call.
    """

    @abstractmethod
    def plan(self, args: dict[str, Any]) -> WritePlan | ToolResult:
        """Validate inputs and produce a WritePlan. **No disk write.**

        On validation failure return a ``ToolResult(is_error=True)`` instead of
        raising — keeps the dispatch contract uniform.
        """
        ...

    @abstractmethod
    def apply(self, plan: WritePlan) -> ToolResult:
        """Commit the plan to disk. Should be small and side-effect-only."""
        ...

    def run(self, args: dict[str, Any]) -> ToolResult:
        outcome = self.plan(args)
        if isinstance(outcome, ToolResult):
            return outcome
        return self.apply(outcome)
