"""coding_harness CLI.

Usage:
    bjorn                                                # GUI for cwd in a browser tab
    python -m coding_harness "your prompt here"          # one-shot (print mode)
    python -m coding_harness -i                          # multi-turn REPL
    python -m coding_harness serve --port 9100           # HTTP+SSE server
    bjorn brain status | search <query> | index          # the second-brain index
    python -m coding_harness --model gpt-oss:20b -i      # alt local model

Modes:
    gui    (no args)  serve the web UI for cwd on a free loopback port, open it
    print  (prompt)   one prompt → events on stderr + final text on stdout, exit
    repl   (-i)       stdin loop, /exit or Ctrl-D to end, shared session history
    serve  (subcmd)   HTTP API on --port; SSE stream + OpenAPI; default plan
    brain  (subcmd)   status, search or re-index the configured second brain;
                      exit 0 ok, 1 stale or not ok, 2 misconfigured
    trust  (subcmd)   trust a directory so its settings, MCP config and
                      sentinel hook take effect

Banned-origin models (README, Model policy) are refused at the
CLI before any session starts. CI test ``tests/test_banned_models.py`` enforces
the same blocklist.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from coding_harness.core import model_roles
from coding_harness.core.mode import Autonomy, parse_autonomy
from coding_harness.core.settings import Settings, SettingsError, load_settings
from coding_harness.models.ollama import BannedModelError, assert_model_allowed
from coding_harness.modes import exec_output, print_mode, repl_mode

_MODE_ALIAS = {"plan": Autonomy.OFF, "act": Autonomy.LOW}


def _settings() -> Settings | None:
    """Merged settings for cwd, adopted as this process's model roles; None on a bad file."""
    try:
        settings = load_settings(os.getcwd())
        model_roles.configure(settings)
        _trust_notice(settings)
        return settings
    except SettingsError as e:
        print(f"error: settings: {e}", file=sys.stderr)
        return None


def _trust_notice(settings: Settings) -> None:
    """Say on stderr what this directory supplies that is skipped for lack of trust."""
    from coding_harness.core import trust
    from coding_harness.mcp.config import find_config
    from coding_harness.security.hook_adapter import _BUNDLED, HOOK_REL

    cwd = Path(os.getcwd()).resolve()
    skipped = [f"settings keys {', '.join(settings.untrusted_keys)}"] if settings.untrusted_keys else []
    mcp = find_config(cwd)
    if mcp is not None and not trust.is_trusted(mcp.parent):
        skipped.append(str(mcp))
    hooks = [d / HOOK_REL for d in (cwd, *cwd.parents) if (d / HOOK_REL).is_file() and (d / HOOK_REL).resolve() != _BUNDLED]
    skipped += [str(h) for h in hooks if not trust.is_trusted(h.parents[2])]
    if skipped:
        print(f"note: untrusted project, skipped: {'; '.join(skipped)}. "
              "Review them, then run `bjorn trust` to enable.", file=sys.stderr)


def _run_trust(argv: list[str]) -> int:
    """Handle ``bjorn trust [DIR]``: record a directory as trusted."""
    from coding_harness.core import trust

    parser = argparse.ArgumentParser(
        prog="bjorn trust",
        description="Trust a directory so its .bjorn/settings.json, .mcp.json and "
                    "sentinel hook take effect. Covers everything beneath it.",
    )
    parser.add_argument("directory", nargs="?", default=".", help="directory to trust (default: cwd)")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.directory):
        print(f"error: not a directory: {args.directory}", file=sys.stderr)
        return 2
    print(f"trusted: {trust.trust(Path(args.directory))}")
    return 0


def _autonomy_arg(args: argparse.Namespace) -> Autonomy | None:
    """--autonomy, else the --mode alias, else None (settings or LOW decide)."""
    if args.autonomy is not None:
        return parse_autonomy(args.autonomy)
    if args.mode is not None:
        return _MODE_ALIAS[args.mode]
    return None


def _resolve_model(flag: str | None, settings: Settings) -> tuple[str, bool]:
    """(model, explicit): --model, else the settings' code role or model key, else the default."""
    chosen = flag or settings.models.get("code") or settings.model
    if chosen is None:
        return print_mode.DEFAULT_MODEL, False
    return chosen, True


def _run_serve(argv: list[str]) -> int:
    """Handle ``coding_harness serve ...`` invocations."""
    from coding_harness.modes import serve_mode

    parser = argparse.ArgumentParser(
        prog="bjorn serve",
        description="HTTP+SSE serve mode for coding_harness.",
    )
    parser.add_argument(
        "--host", default=serve_mode.DEFAULT_HOST,
        help=f"bind host (default {serve_mode.DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port", type=int, default=serve_mode.DEFAULT_PORT,
        help=(f"bind port (default {serve_mode.DEFAULT_PORT}); "
              "0 = pick any free port"),
    )
    parser.add_argument(
        "--model", default=None,
        help=f"Ollama model (default {print_mode.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--force-local", action="store_true",
        help="dispatch every turn to local Ollama, skip router",
    )
    parser.add_argument(
        "--no-mcp", action="store_true",
        help="skip loading MCP servers from .mcp.json",
    )
    parser.add_argument(
        "--ui-origin", default=serve_mode.DEFAULT_UI_ORIGIN,
        help=(f"browser origin allowed via CORS "
              f"(default {serve_mode.DEFAULT_UI_ORIGIN})"),
    )
    parser.add_argument(
        "--frontier-backend", choices=["api", "claude-cli"], default="api",
        help=("backend for high-complexity frontier turns: 'api' (raw API "
              "key, non-streaming) or 'claude-cli' (Max-plan quota, text-only, "
              "streaming). Default api."),
    )
    args = parser.parse_args(argv)

    settings = _settings()
    if settings is None:
        return 2
    model, explicit_model = _resolve_model(args.model, settings)
    try:
        assert_model_allowed(model)
    except BannedModelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    from coding_harness.modes import serve_auth
    for line in serve_auth.startup_lines(args.host):
        print(f"[coding_harness] {line}", file=sys.stderr, flush=True)
    return serve_mode.run(
        host=args.host,
        port=args.port,
        model=model,
        force_local=args.force_local,
        explicit_model=explicit_model,
        enable_mcp=not args.no_mcp,
        ui_origin=args.ui_origin,
        frontier_backend=args.frontier_backend,
        settings=settings,
    )


def _run_brain(argv: list[str]) -> int:
    """Handle ``bjorn brain ...``: status, search and index for the configured brain."""
    from coding_harness.modes import brain_cmd

    return brain_cmd.run(argv, _settings)


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "serve":
        return _run_serve(argv[1:])
    if argv and argv[0] == "brain":
        return _run_brain(argv[1:])
    if argv and argv[0] == "trust":
        return _run_trust(argv[1:])
    parser = argparse.ArgumentParser(
        prog="bjorn",
        description="bjorn: local-first coding agent with a security gate, on Ollama.",
    )
    parser.add_argument(
        "prompt",
        nargs="?",
        help="User prompt for one-shot (print) mode. Omit it to open the GUI, "
             "or pass -i for the terminal REPL.",
    )
    parser.add_argument(
        "--no-open",
        action="store_true",
        help="(GUI) Print the URL instead of opening a browser tab.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="(GUI) Loopback port to serve on (default: any free port).",
    )
    parser.add_argument(
        "-i", "--interactive",
        action="store_true",
        help="Multi-turn REPL session — read prompts from stdin until /exit or Ctrl-D.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="(REPL only) Stream raw JSONL events on stderr instead of the "
             "pretty renderer. Useful for piping or debugging.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"Ollama model name (default: {print_mode.DEFAULT_MODEL}, or the "
             "settings file's \"model\" key). "
             "Passing this explicitly bypasses the router (treated as "
             "override_explicit_model in the audit log). "
             "Banned-origin models are refused (README, Model policy).",
    )
    parser.add_argument(
        "--force-local",
        action="store_true",
        help="Skip the router entirely and dispatch every turn to the local "
             "Ollama model (--model). Useful when you want hermetic local "
             "behavior regardless of query content.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="(print mode) Refuse every Write/Edit and feed the diff back to "
             "the model as the tool result. Disk is never touched.",
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip loading MCP servers from .mcp.json. Built-in tools "
             "(Read/Bash/Write/Edit/Grep/Glob) only. Useful for tests, "
             "offline sessions, or when an MCP server is misbehaving.",
    )
    parser.add_argument(
        "--autonomy",
        choices=["off", "low", "medium", "high"],
        default=None,
        help="Autonomy level. off = read-only (Plan mode); low = edits under "
             "cwd plus tests, lint and build checks (default); medium adds "
             "commits, installs and file ops inside cwd; high adds network "
             "and everything the blocklist leaves alone. Commands outside the "
             "level ask the operator in the REPL and are denied headless. "
             "A settings file (~/.config/bjorn or .bjorn) may set the default.",
    )
    parser.add_argument(
        "--mode",
        choices=["plan", "act"],
        default=None,
        help="Deprecated alias for --autonomy: plan = off, act = low. "
             "--autonomy wins when both are given.",
    )
    parser.add_argument(
        "--max-time",
        type=float,
        default=None,
        metavar="SECONDS",
        help="(print mode) Wall-clock budget for the turn. When it runs out "
             "the turn halts with halted_reason=deadline and reports the "
             "files it changed instead of discarding them.",
    )
    parser.add_argument(
        "--check",
        action="append",
        default=[],
        metavar="CMD",
        help="(print mode) Shell command the green-before-done gate runs "
             "before the turn finishes; repeatable. Without it the gate "
             "discovers the repo's test command. HARNESS_DONE_GATE=0 disables.",
    )
    parser.add_argument(
        "--worktree",
        default=None,
        metavar="TOPIC",
        help="Create ../<repo>-<topic> (git worktree add, -b unless the "
             "branch exists), chdir there, and start the REPL (implies -i).",
    )
    parser.add_argument(
        "--resume",
        default=None,
        metavar="SESSION_ID",
        help="(REPL) Continue a prior session from its transcript.",
    )
    parser.add_argument(
        "--continue",
        dest="cont",
        action="store_true",
        help="(REPL) Continue the newest session started in this directory.",
    )
    parser.add_argument(
        "--output-format",
        choices=exec_output.FORMATS,
        default="text",
        help="(print mode) text prints the final answer; json prints one "
             "result envelope on stdout and exits 0 done, 3 deadline, "
             "4 max_turns or stuck, 5 error, 6 interrupted.",
    )
    args = parser.parse_args(argv)
    if args.max_time is not None and args.max_time <= 0:
        parser.error("--max-time must be a positive number of seconds")
    if (args.resume or args.cont) and not args.interactive:
        parser.error("--resume and --continue are REPL only (pass -i)")
    if args.worktree is not None:
        if args.prompt:
            parser.error("--worktree starts the REPL; do not pass a prompt")
        args.interactive = True
        from coding_harness.modes.worktree import WorktreeError, enter
        try:
            path = enter(args.worktree, os.getcwd())
        except WorktreeError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"worktree: {path}", file=sys.stderr)

    settings = _settings()
    if settings is None:
        return 2
    model, explicit_model = _resolve_model(args.model, settings)
    try:
        assert_model_allowed(model)
    except BannedModelError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    autonomy = _autonomy_arg(args)

    if args.interactive:
        if args.prompt:
            print(
                "error: pass either a prompt OR -i/--interactive, not both",
                file=sys.stderr,
            )
            return 2
        if args.dry_run:
            print(
                "error: --dry-run is print-mode only; REPL always confirms "
                "interactively",
                file=sys.stderr,
            )
            return 2
        return repl_mode.run(
            model=model,
            verbose=args.verbose,
            force_local=args.force_local,
            explicit_model=explicit_model,
            enable_mcp=not args.no_mcp,
            autonomy=autonomy,
            settings=settings,
            resume=args.resume,
            continue_latest=args.cont,
        )

    if not args.prompt:
        from coding_harness.modes import ui_mode
        return ui_mode.run(
            model=model,
            explicit_model=explicit_model,
            force_local=args.force_local,
            enable_mcp=not args.no_mcp,
            settings=settings,
            port=args.port if args.port is not None else ui_mode.DEFAULT_UI_PORT,
            open_browser=not args.no_open,
        )

    return print_mode.run(
        args.prompt,
        model=model,
        dry_run=args.dry_run,
        force_local=args.force_local,
        explicit_model=explicit_model,
        enable_mcp=not args.no_mcp,
        max_time_s=args.max_time,
        autonomy=autonomy,
        settings=settings,
        checks=args.check,
        output_format=args.output_format,
    )


if __name__ == "__main__":
    sys.exit(main())
