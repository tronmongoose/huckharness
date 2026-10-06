"""REPL mode: multi-turn interactive session.

Reads one prompt at a time from stdin, runs the agent loop on it, prints the
assistant's final text to stdout, then loops. Same Session for the lifetime
of the REPL: one session_id, one audit chain head, one conversation history
shared across turns.

The MAX_TURNS cap in the session loop is per-prompt, not per-session, so
each user line gets its own 25-turn agent budget. History past the
compaction threshold is folded into a local-model summary each turn.

Default UI is colored / collapsed via ``_pretty``, with the model's text
streamed to stderr as it arrives. ``--verbose`` switches to the JSONL
stderr stream the print mode uses, so scripts and debugging can still see
every event.

Ctrl-C during a turn interrupts that turn and returns to the prompt; Ctrl-C
at the prompt ends the session, as does EOF (Ctrl-D). ``--resume`` and
``--continue`` rebuild history from a prior transcript before the first
prompt. Slash commands live in ``repl_commands``.
"""
from __future__ import annotations

import json
import os
import sys
from functools import partial
from pathlib import Path
from typing import Any

from coding_harness.context.skills import load_skill
from coding_harness.core import session as session_mod
from coding_harness.core import transcripts
from coding_harness.core.hooks import HookRunner
from coding_harness.core.hooks import build_runner as build_hook_runner
from coding_harness.core.mode import Autonomy, mode_for
from coding_harness.core.permissions import PermissionBroker
from coding_harness.core.replay import rebuild_messages
from coding_harness.core.session import Session, SessionResult
from coding_harness.core.settings import Settings, load_settings, resolve_autonomy
from coding_harness.modes import _pretty as pretty
from coding_harness.modes import custom_commands, repl_commands, repl_input
from coding_harness.modes.interruptible import run_interruptible
from coding_harness.modes.print_mode import (
    DEFAULT_MODEL,
    build_registry,
    build_system_prompt,
    stderr_event_sink,
)
from coding_harness.security import audit

_HELP_TEXT = repl_commands.HELP_TEXT
_MAX_PROMPTS = 1_000_000


class _ConsoleBroker(PermissionBroker):
    """Broker answered at the terminal. The y/N prompt runs inside ``emit``,
    so the decision is resolved before ``request`` ever waits on it."""

    def __init__(self) -> None:
        super().__init__(emit=self._emit)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        """Ask the operator about a permission_request; ignore other events."""
        if kind != "permission_request":
            return
        with self.lock:
            req = self.pending.get(payload["req_id"])
        args = req.args if req is not None else payload.get("args_preview", {})
        approved = pretty.confirm_command(payload["tool"], args, payload["reason"])
        self.resolve(payload["req_id"], "allow_once" if approved else "deny")


def _say(text: str) -> None:
    print(text, file=sys.stderr, flush=True)




def find_transcript(session_id: str | None, cwd: str) -> Path | None:
    """The transcript for ``session_id``, or the newest one started in ``cwd``."""
    sessions_dir = session_mod.SESSIONS_DIR
    if session_id is not None:
        path = sessions_dir / f"{session_id}.jsonl"
        return path if path.exists() else None
    newest = transcripts.recent(cwd, limit=1)
    return newest[0] if newest else None


def _restore(session: Session, path: Path) -> int | None:
    """Replay ``path`` into ``session``; the user-turn count, or None if unusable."""
    replayed = rebuild_messages(path, session.system_prompt)
    for warning in replayed.warnings:
        _say(f"  resume: {warning}")
    if not replayed.complete:
        return None
    session.restore(replayed.messages, user_turns=replayed.user_turns,
                    sensitive=replayed.sensitive)
    try:
        audit.append_session_resume(
            session_id=session.session_id,
            messages=len(replayed.messages),
            user_turns=replayed.user_turns,
            agent_identity=session.agent_identity,
        )
    except Exception as e:  # noqa: BLE001 telemetry must not block resume
        session._log("resume_audit_error", {"error": f"{type(e).__name__}: {e}"})
    return replayed.user_turns


def _print_result(result: SessionResult, sink: Any, verbose: bool) -> None:
    """Final text to stdout, unless the pretty sink already streamed it to a tty."""
    if result.final_text:
        streamed = (
            not verbose
            and sys.stdout.isatty()
            and getattr(sink, "streamed_text", "").strip() == result.final_text.strip()
        )
        print()
        if not streamed:
            print(result.final_text)
    if result.halted_reason == "interrupted" and not verbose:
        _say("  · interrupted")
    if verbose:
        footer = {
            "event": "turn_done",
            "turns": result.turns,
            "halted_reason": result.halted_reason,
        }
        if result.error:
            footer["error"] = result.error
        _say(json.dumps(footer))


_UNKNOWN_SKILL = object()


def _expand_command(line: str):
    """A custom command or /skill expands to a model prompt; None otherwise.

    Returns _UNKNOWN_SKILL when /skill names a skill that does not exist so
    the caller can report it rather than sending the literal line to the model.
    """
    if line.startswith("/skill "):
        text = load_skill(line.split(None, 1)[1].strip(), cwd=os.getcwd())
        return text if text is not None else _UNKNOWN_SKILL
    return custom_commands.expand(line)


def _loop(  # LONG-FN: the prompt loop plus the hook lifecycle around each turn
    session: Session,
    sink: Any,
    state: repl_commands.ReplState,
    verbose: bool,
    hooks: HookRunner | None = None,
) -> int:
    """Prompt, dispatch, print, until EOF or /exit. Returns the exit code."""
    exit_code = 0
    for _ in range(_MAX_PROMPTS):
        line = repl_input.read_prompt(pretty.prompt_string())
        if line is None:
            # Newline so the shell prompt that follows is not on the same
            # line as the harness prompt.
            print(file=sys.stderr)
            return exit_code
        line = line.strip()
        if not line:
            continue
        injected = _expand_command(line)
        if injected is _UNKNOWN_SKILL:
            print("no such skill", file=sys.stderr)
            continue
        if injected is not None:
            line = injected
        else:
            verdict = repl_commands.handle(session, line, state)
            if verdict == "exit":
                return exit_code
            if verdict == "handled":
                continue
        if hooks is not None:
            hooks.run("UserPromptSubmit")
        result = run_interruptible(session, partial(session.run_turn, line))
        state.record(result)
        _print_result(result, sink, verbose)
        if hooks is not None:
            stop = hooks.run("Stop")
            if stop.stop_messages:
                feedback = "\n".join(stop.stop_messages)
                result = run_interruptible(session, partial(session.run_turn, feedback))
                state.record(result)
                _print_result(result, sink, verbose)
        if result.error:
            exit_code = 1
    return exit_code


def _welcome(session: Session, model: str, verbose: bool) -> None:
    if verbose:
        _say(f"[coding_harness] REPL: model={model} session={session.session_id}")
        _say("[coding_harness] /exit or Ctrl-D to quit, /help for commands")
    else:
        pretty.print_banner(model=model, session_id=session.session_id)


def _farewell(session: Session, verbose: bool) -> None:
    if verbose:
        _say(json.dumps({
            "event": "summary",
            "session_id": session.session_id,
            "session_log": str(session.session_log_path),
        }))
    else:
        pretty.print_goodbye(
            session_id=session.session_id, total_turns=session.total_turns,
        )


def run(  # LONG-FN: wiring in one place; the loop and commands live elsewhere
    *,
    model: str = DEFAULT_MODEL,
    verbose: bool = False,
    force_local: bool = False,
    explicit_model: bool = False,
    enable_mcp: bool = True,
    autonomy: Autonomy | None = None,
    settings: Settings | None = None,
    resume: str | None = None,
    continue_latest: bool = False,
) -> int:
    cwd = os.getcwd()
    if settings is None:
        settings = load_settings(cwd)
    level = resolve_autonomy(autonomy, settings)
    sink = stderr_event_sink if verbose else pretty.PrettySink()

    transcript = None
    if resume is not None or continue_latest:
        transcript = find_transcript(resume, cwd)
        if transcript is None:
            what = f"session {resume}" if resume else f"a session started in {cwd}"
            print(f"error: no transcript for {what}", file=sys.stderr)
            return 2

    # A person is at the prompt, so the second brain is on (print mode, which
    # the fleet drives, never gets it).
    registry = build_registry(event_sink=sink, enable_mcp=enable_mcp, brain_block=settings.brain,
                              subagents=True, settings=settings)
    # REPL is interactive by definition: every Write/Edit pauses for diff
    # confirmation, and a command the level does not cover pauses for y/N.
    # Print mode wires its own callback (auto-accept or dry-run) and no broker.
    registry.confirm_callback = pretty.confirm_diff
    registry.autonomy = level
    registry.settings = settings
    registry.permission_broker = _ConsoleBroker()
    session_kwargs: dict[str, Any] = {}
    if transcript is not None:
        session_kwargs["session_id"] = transcript.stem
    session = Session(
        model=model,
        registry=registry,
        system_prompt=build_system_prompt(),
        event_sink=sink,
        force_local=force_local,
        explicit_model=explicit_model,
        paste_echo_callback=pretty.confirm_paste_echo,
        mode=mode_for(level),
        **session_kwargs,
    )
    session.recall_hints = True
    if transcript is not None:
        turns = _restore(session, transcript)
        if turns is None:
            print(f"error: cannot resume {transcript.stem}", file=sys.stderr)
            session.close()
            return 2
        _say(f"  resumed session {session.session_id} · {turns} turn(s)")

    hooks = build_hook_runner(settings, session_id=session.session_id, cwd=cwd)
    registry.hooks = hooks
    if hooks is not None:
        hooks.run("SessionStart")

    _welcome(session, model, verbose)
    repl_input.load_history()
    state = repl_commands.ReplState()
    exit_code = 0
    try:
        exit_code = _loop(session, sink, state, verbose, hooks)
    except KeyboardInterrupt:
        _say("\n[coding_harness] interrupted")
        exit_code = 130
    finally:
        repl_input.save_history()
        session.close()
        _farewell(session, verbose)
    return exit_code
