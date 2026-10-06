"""Print mode — one-shot: prompt in, events + final text to stdout.

Events stream as JSONL on stderr (so they don't pollute the captured assistant
text on stdout). The final assistant text is printed to stdout at the end.

This mirrors pi-mono's ``print-mode`` shape: anything that wants the result
parses stdout; anything that wants progress tails stderr.

The ``build_registry`` and system-prompt helpers are exported for reuse by
``repl_mode`` and any future mode that wants the same default tool surface.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

from coding_harness.context.brain import BrainError, client_for
from coding_harness.context.conventions import render_conventions
from coding_harness.context.memory import memory_dir
from coding_harness.context.overlay import render_overlay
from coding_harness.context.repomap import build_repo_map
from coding_harness.context.skills import skills_block
from coding_harness.context.toolprobe import probe, render
from coding_harness.core.envelope import preset
from coding_harness.core.hooks import build_runner as build_hook_runner
from coding_harness.core.mode import Autonomy, mode_for
from coding_harness.core.session import Session
from coding_harness.core.settings import Settings, load_settings, resolve_autonomy
from coding_harness.modes import exec_output
from coding_harness.modes.interruptible import run_interruptible
from coding_harness.tools.base import WritePlan
from coding_harness.tools.bash import Bash
from coding_harness.tools.brain import Brain
from coding_harness.tools.edit import Edit
from coding_harness.tools.explore import Explore
from coding_harness.tools.glob_tool import Glob
from coding_harness.tools.grep import Grep
from coding_harness.tools.read import Read
from coding_harness.tools.registry import DispatchEvent, ToolRegistry
from coding_harness.tools.todo import TodoWrite
from coding_harness.tools.write import Write

DEFAULT_MODEL = "mistral-small3.2:latest"


def _repo_map_budget() -> int:
    try:
        return max(0, int(os.environ.get("HARNESS_REPO_MAP_TOKENS", "1024")))
    except ValueError:
        return 1024


def build_system_prompt(cwd: str | None = None, *, include_repo_map: bool = True) -> str:
    """Base prompt + repo conventions + a ranked repo map for ``cwd``.

    Built per-session (not frozen at import) so a nightshift worktree ingests
    its own AGENTS.md/CLAUDE.md and its own symbol map rather than the
    CLI-invocation directory's. Repo map kill switch: HARNESS_REPO_MAP=0.
    Tool probe kill switch: HARNESS_TOOL_PROBE=0.
    ``include_repo_map=False`` skips the map build — used for the frozen
    SYSTEM_PROMPT fallback so importing this module stays cheap.
    """
    if cwd is None:
        cwd = os.getcwd()
    base = f"""You are a coding assistant running in a local fallback harness.

Working directory: {cwd}
All relative paths the user mentions are relative to this directory.

You have six tools:

  - Bash: run a shell command (use this for `git`, `pwd`, `make`, `npm`,
    and anything that needs shell execution). Bash output may include
    relative paths.

  - Read: read ONE file from disk. The `file_path` argument MUST be an
    absolute path starting with `/`. Read does NOT accept directories or
    relative paths. If you only have a relative path, prefix it with the
    working directory above, or use Bash with `realpath` to resolve it.

  - Write: create or overwrite a file. The `file_path` argument MUST be an
    absolute path. Use this to create new files. Parent directories are
    created automatically.

  - Edit: find-and-replace in an existing file. Provide `old_string` (the
    exact text to find) and `new_string` (the replacement). old_string must
    match exactly including whitespace. Fails if old_string is not found or
    appears more than once (use replace_all=true for global replacement).
    Prefer Edit over Write for modifying existing files — it's safer.

  - Grep: search file contents for a regex pattern. Returns matching file
    paths by default (output_mode="files_with_matches"), or matching lines
    with line numbers (output_mode="content"). Supports glob filtering and
    context lines. Uses ripgrep when available.

  - Glob: find files by glob pattern (e.g. "**/*.py", "src/**/*.ts").
    Returns matching paths sorted by modification time (newest first).

Tool selection rules:
  - Finding files by name pattern → Glob.
  - Searching file contents → Grep.
  - Reading a known file → Read with an absolute path.
  - Creating a new file → Write with an absolute path.
  - Modifying an existing file → Edit (preferred) or Write (full overwrite).
  - Shell commands, git, directory listings → Bash.
  - Never call Read on a directory. Never invent paths you have not seen.
  - Read a file BEFORE editing it — you need to see the exact content to
    match old_string correctly.

Every tool call is reviewed by a security gate before execution. Some commands
(destructive operations, writes to sensitive vaults) will be BLOCKED — when
that happens, the tool result will say so and you should adjust your approach
rather than retry the same command.

Persistent memory for this repository lives in {memory_dir(cwd)}. Its
MEMORY.md index, when present, is injected below under "## Memory". When
you learn something a later session will need that is not derivable from
the code or git history (a constraint, a decision and its reason, a
correction from the operator), write it there with the Write tool: one
fact per file named <type>_<slug>.md where type is feedback, project,
reference or user, YAML frontmatter with `name`, `description` and
`metadata.type`, then add a one-line pointer to MEMORY.md. A memory names
a moment in time: read the file before acting on it.

Do not announce future tool calls — execute them. If you say "let me check
X next", actually call the tool to check X in the same turn. Only stop calling
tools when you have the final answer to give the user.

Be terse. Use tools to gather information rather than guessing. When you have
enough information to answer the user, stop calling tools and respond directly.
"""
    prompt = base + render_overlay() + render_conventions(cwd) + render(probe(cwd)) + skills_block(cwd)
    if include_repo_map and os.environ.get("HARNESS_REPO_MAP", "1") != "0":
        prompt += build_repo_map(cwd, _repo_map_budget())
    return prompt


# Frozen default for back-compat. Built without the repo map so importing this
# module stays cheap — the live modes call build_system_prompt() at session
# construction, which adds the map (and picks up the per-worktree cwd).
SYSTEM_PROMPT = build_system_prompt(include_repo_map=False)


def stderr_event_sink(event: DispatchEvent) -> None:
    line = json.dumps({"event": event.kind, **event.payload}, default=str, ensure_ascii=False)
    print(line, file=sys.stderr, flush=True)


def build_registry(
    event_sink=stderr_event_sink,
    *,
    enable_mcp: bool = True,
    brain_block: dict | None = None,
    subagents: bool = False,
    settings: Settings | None = None,
) -> ToolRegistry:
    """Construct a ToolRegistry with the harness's default tool surface.

    Both print_mode and repl_mode use this. Future modes that want a
    different toolset (sandboxed, read-only, etc.) can compose their own
    registry instead of calling this.

    When ``enable_mcp`` is True (the default) every server in ``.mcp.json``
    is spawned and its tools are registered. A failure to spawn or speak
    MCP is logged on the event sink as ``mcp_load_error`` and execution
    continues with built-ins only — losing MCP shouldn't be fatal to a
    purely-local task. The CLI ``--no-mcp`` flag short-circuits this.
    """
    registry = ToolRegistry(event_sink=event_sink)
    registry.register(Read())
    registry.register(Bash())
    registry.register(Write())
    registry.register(Edit())
    registry.register(Grep())
    registry.register(Glob())
    registry.register(TodoWrite(lambda: registry.session_id))
    _attach_brain(registry, brain_block, event_sink)
    if subagents:
        registry.register(Explore(settings))
    if enable_mcp:
        _attach_mcp_servers(registry, event_sink)
    return registry


def _attach_brain(registry: ToolRegistry, brain_block: dict | None, event_sink) -> None:
    """Register the Brain tool; a brain that cannot start costs the session its brain, not its start.

    The reason lands on ``registry.brain_error``. With an event sink (print,
    REPL) it is also one stderr line here; serve, which builds registries
    without a sink, emits ``brain_unavailable`` once the session exists.
    """
    try:
        brain_client = client_for(brain_block or {})
    except BrainError as e:
        registry.brain_error = str(e)
        if event_sink is not None:
            print(f"warning: second brain unavailable: {e}", file=sys.stderr, flush=True)
        return
    if brain_client is not None:
        registry.register(Brain(brain_client))


def _attach_mcp_servers(registry: ToolRegistry, event_sink) -> None:
    """Best-effort: load .mcp.json, spawn each declared server, register
    its tools. Any failure is logged and skipped; built-ins remain usable.
    """
    from coding_harness.mcp import MCPClient, MCPClientError, load_mcp_config
    from coding_harness.tools.registry import DispatchEvent

    def _emit(kind: str, payload: dict[str, Any]) -> None:
        if event_sink is not None:
            event_sink(DispatchEvent(kind=kind, payload=payload))

    try:
        servers = load_mcp_config()
    except (OSError, ValueError) as e:
        _emit("mcp_load_error", {"stage": "config", "error": str(e)})
        return

    for server in servers:
        client = MCPClient(server)
        try:
            tool_names = registry.register_mcp_server(client)
        except MCPClientError as e:
            _emit("mcp_load_error", {
                "stage": "register",
                "server": server.name,
                "error": str(e),
            })
            try:
                client.close()
            except Exception:
                pass
            continue
        _emit("mcp_server_registered", {
            "server": server.name,
            "tools": tool_names,
        })


def _auto_accept(_: WritePlan) -> bool:
    return True


def _dry_run_deny(plan: WritePlan) -> bool:
    """Refuse every write; the registry returns the diff in the tool result so
    the model can summarize what *would* have happened."""
    return False


def run(  # LONG-FN: one-shot wiring in one place, incl. the hook lifecycle
    user_prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
    force_local: bool = False,
    explicit_model: bool = False,
    enable_mcp: bool = True,
    max_time_s: float | None = None,
    autonomy: Autonomy | None = None,
    settings: Settings | None = None,
    checks: list[str] | None = None,
    output_format: str = "text",
) -> int:
    cwd = os.getcwd()
    started = time.monotonic()
    if settings is None:
        settings = load_settings(cwd)
    level = resolve_autonomy(autonomy, settings)

    registry = build_registry(enable_mcp=enable_mcp)
    registry.confirm_callback = _dry_run_deny if dry_run else _auto_accept
    registry.autonomy = level
    registry.settings = settings

    # Headless sessions get the cwd preset and no broker, so an out-of-scope
    # call (or an 'ask' from the ladder) denies at once. HARNESS_ENVELOPE=off
    # restores the ungated session.
    envelope = None if os.environ.get("HARNESS_ENVELOPE") == "off" else preset(cwd, level, settings)
    session = Session(
        model=model,
        registry=registry,
        system_prompt=build_system_prompt(),
        event_sink=stderr_event_sink,
        force_local=force_local,
        explicit_model=explicit_model,
        mode=mode_for(level),
        envelope=envelope,
        done_checks=list(checks or []),
    )

    hooks = build_hook_runner(
        settings, session_id=getattr(session, "session_id", "unknown"), cwd=cwd,
    )
    registry.hooks = hooks
    if hooks is None:
        result = run_interruptible(
            session, lambda: session.run(user_prompt, deadline_s=max_time_s),
        )
    else:
        # run_turn (not run) so a Stop-hook block can feed back one extra
        # turn before the session closes; the no-hooks path is unchanged.
        hooks.run("SessionStart")
        hooks.run("UserPromptSubmit")
        result = run_interruptible(
            session, lambda: session.run_turn(user_prompt, deadline_s=max_time_s),
        )
        stop = hooks.run("Stop")
        if stop.stop_messages:
            feedback = "\n".join(stop.stop_messages)
            result = run_interruptible(
                session, lambda: session.run_turn(feedback, deadline_s=max_time_s),
            )
        session.close()

    if output_format == "json":
        body = exec_output.envelope(
            result, model=model, autonomy=level.name.lower(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        print(json.dumps(body), flush=True)
        return exec_output.exit_code(result.halted_reason)

    # Final assistant text → stdout.
    print(result.final_text)

    # Footer summary → stderr.
    summary: dict[str, Any] = {
        "event": "summary",
        "session_id": result.session_id,
        "turns": result.turns,
        "halted_reason": result.halted_reason,
        "files_changed": result.files_changed,
        "checks_passed": result.checks_passed,
        "session_log": str(result.session_log_path),
    }
    if result.error:
        summary["error"] = result.error
    print(json.dumps(summary), file=sys.stderr, flush=True)

    return 0 if result.halted_reason == "model_done" else 1
