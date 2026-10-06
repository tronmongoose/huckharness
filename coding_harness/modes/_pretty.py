"""Human-readable event renderer for the REPL.

The default REPL stderr sink streams JSONL — great for scripts, terrible
for sitting at a terminal. This module collapses each tool dispatch's
event quartet (``sentinel_verdict`` → ``tool_call_start`` →
``tool_call_result`` → ``audit_entry``) into one compact, ANSI-colored
line that mirrors Claude Code's interactive feel:

  ⎿ Bash  git status                            (per dispatch, after result)
  ⊘ Bash  rm -rf /  ·  blocked: destructive    (red, on Sentinel deny)
  ⚠ ollama transport error                     (yellow, on tool/loop error)

The session log on disk still gets the full JSONL stream — only the live
screen rendering changes. Power users / scripts can keep the JSONL stderr
output via ``--verbose``.

Color usage follows ``NO_COLOR`` (https://no-color.org/) and TTY detection.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

from coding_harness.tools.base import WritePlan
from coding_harness.tools.registry import DispatchEvent

# ── Palette ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Palette:
    reset: str = ""
    dim: str = ""
    bold: str = ""
    red: str = ""
    green: str = ""
    yellow: str = ""
    blue: str = ""
    cyan: str = ""
    magenta: str = ""


_NO_COLOR = _Palette()
_ANSI = _Palette(
    reset="\033[0m",
    dim="\033[2m",
    bold="\033[1m",
    red="\033[31m",
    green="\033[32m",
    yellow="\033[33m",
    blue="\033[34m",
    cyan="\033[36m",
    magenta="\033[35m",
)


def _color_enabled(stream=sys.stderr) -> bool:
    if "NO_COLOR" in os.environ:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    # Both stdout and stderr should be TTYs for a clean experience —
    # the prompt lands on stdout, events on stderr; if either is piped,
    # downgrade to plain.
    try:
        return stream.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001
        return False


def palette(stream=sys.stderr) -> _Palette:
    return _ANSI if _color_enabled(stream) else _NO_COLOR


# ── Banner ──────────────────────────────────────────────────────────

BANNER_COMMANDS = (
    "/help · /exit · /autonomy · /mode · /plan · /act · /model · /rewind · "
    "/compact · /context · /diff · Ctrl-D"
)

# Small text logo. Renders fine on any UTF-8 terminal; `cyan` color is
# applied at print time when the terminal supports it.
_LOGO_LINES = [
    "┌─┐┌─┐┌┬┐┬┌┐┌┌─┐    ┬ ┬┌─┐┬─┐┌┐┌┌─┐┌─┐┌─┐",
    "│  │ │ │││││││ ┬    ├─┤├─┤├┬┘│││├┤ └─┐└─┐",
    "└─┘└─┘─┴┘┴┘└┘└─┘    ┴ ┴┴ ┴┴└─┘└┘└─┘└─┘└─┘",
]


def print_banner(*, model: str, session_id: str, stream=sys.stderr) -> None:
    p = palette(stream)
    print(file=stream)
    for line in _LOGO_LINES:
        print(f"  {p.cyan}{line}{p.reset}", file=stream)
    print(
        f"  {p.dim}local fallback · model{p.reset}  {model}",
        file=stream,
    )
    print(
        f"  {p.dim}session{p.reset}  {p.dim}{session_id}{p.reset}",
        file=stream,
    )
    print(file=stream)
    print(
        f"  {p.dim}{BANNER_COMMANDS}{p.reset}",
        file=stream,
        flush=True,
    )


# ── Prompt ──────────────────────────────────────────────────────────


def prompt_string(stream=sys.stderr) -> str:
    """The prompt glyph the REPL writes to stderr before reading a line."""
    if not _color_enabled(stream):
        return "\n❯ "
    p = palette(stream)
    return f"\n{p.cyan}❯{p.reset} "


# ── Goodbye ─────────────────────────────────────────────────────────


def print_goodbye(*, session_id: str, total_turns: int, stream=sys.stderr) -> None:
    p = palette(stream)
    print(file=stream)
    print(
        f"  {p.dim}— session {session_id} · {total_turns} turn(s){p.reset}",
        file=stream,
        flush=True,
    )


# ── Pretty event sink ───────────────────────────────────────────────


def _truncate(s: str, n: int) -> str:
    s = s.strip().replace("\n", " ")
    if len(s) <= n:
        return s
    return s[: n - 1] + "…"


_TOOL_ARG_KEY = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
    "Grep": "pattern",
    "Glob": "pattern",
}


_PATH_ARG_KEYS = ("file_path", "path")


def _truncate_path(s: str, n: int) -> str:
    """Keep the tail of a path: the file name matters more than the root."""
    s = s.strip().replace("\n", " ")
    if len(s) <= n:
        return s
    return "..." + s[-n:]


def _summarize_args(tool: str, args: dict) -> str:
    if not isinstance(args, dict):
        return _truncate(repr(args), 80)
    key = _TOOL_ARG_KEY.get(tool)
    if key and key in args:
        if key in _PATH_ARG_KEYS:
            return _truncate_path(str(args[key]), 80)
        return _truncate(str(args[key]), 80)
    if args:
        first_k = next(iter(args))
        return _truncate(f"{first_k}={args[first_k]!r}", 80)
    return ""


class PrettySink:
    """Per-dispatch event collator → one human line per tool call.

    Event order from ``ToolRegistry.dispatch``:
      - approved + tool_ok:    sentinel_verdict → tool_call_start → tool_call_result → audit_entry
      - sentinel-blocked:      sentinel_verdict → audit_entry
      - tool body raised:      sentinel_verdict → tool_call_start → tool_call_result(is_error=True) → audit_entry
      - unknown tool:          audit_entry  (no sentinel_verdict)

    We render on ``audit_entry`` because it's the only event guaranteed to
    fire for every dispatch. Earlier events accumulate into ``_pending``.
    """

    def __init__(self, *, stream=sys.stderr) -> None:
        self.stream = stream
        self._palette = palette(stream)
        self._pending: dict | None = None
        self._turn_started: float | None = None
        self._segment: list[str] = []  # delta text since the last status line
        self.streamed_text = ""  # delta text of the turn's last segment

    def _elapsed(self) -> str:
        if self._turn_started is None:
            return "0.0s"
        return f"{time.monotonic() - self._turn_started:.1f}s"

    def _end_segment(self) -> None:
        """Close an open delta line so the next status line starts fresh."""
        self.streamed_text = "".join(self._segment)
        if self._segment:
            print(file=self.stream, flush=True)
        self._segment = []

    def _status(self, label: str) -> None:
        c = self._palette
        self._end_segment()
        print(f"  {c.dim}· {label} · {self._elapsed()}{c.reset}", file=self.stream, flush=True)

    def _delta(self, text: str) -> None:
        if not text:
            return
        self._segment.append(text)
        self.stream.write(text)
        self.stream.flush()

    def __call__(self, event: DispatchEvent) -> None:  # LONG-FN: one branch per event kind
        kind = event.kind
        payload = event.payload

        # session_start: banner already showed model + tools, suppress.
        # session_done: footer is printed by the REPL on close, suppress.
        # plan_ready: surfaced via confirm callback; dispatch line lands later.
        # snapshot_capture / rewind: REPL prints its own messages.
        if kind in (
            "session_start",
            "session_done",
            "plan_ready",
            "snapshot_capture",
            "rewind",
        ):
            return
        if kind in ("assistant_delta", "turn_start", "turn_done", "error"):
            self._turn_event(kind, payload)
            return
        self._dispatch_event(kind, payload)

    def _turn_event(self, kind: str, payload: dict) -> None:
        """Streamed text and the turn boundaries around it."""
        if kind == "assistant_delta":
            self._delta(payload.get("text", ""))
        elif kind == "turn_start":
            self._turn_started = time.monotonic()
            self.streamed_text = ""
            self._status(f"turn {payload.get('turn', '?')}")
        elif kind == "turn_done":
            self._status(f"done · {payload.get('halted_reason') or 'ok'}")
        else:
            self._end_segment()
            self._render_error(payload)

    def _dispatch_event(self, kind: str, payload: dict) -> None:
        """Accumulate one tool dispatch's events; render on audit_entry."""
        if kind == "sentinel_verdict":
            self._pending = {
                "tool": payload.get("tool", "?"),
                "allowed": payload.get("allowed", False),
                "reason": payload.get("reason", ""),
                "path": payload.get("path", ""),
            }
            return

        if kind == "tool_call_start":
            self._status(payload.get("tool", "?"))
            if self._pending is None:
                self._pending = {"tool": payload.get("tool", "?")}
            # The tool only starts once every gate passed, so an 'ask'
            # verdict the operator approved renders as a run, not a block.
            self._pending["allowed"] = True
            self._pending["args"] = payload.get("args", {})
            return

        if kind == "tool_call_result":
            if self._pending is None:
                self._pending = {"tool": payload.get("tool", "?"), "allowed": True}
            self._pending["is_error"] = payload.get("is_error", False)
            self._pending["denied_by_operator"] = payload.get("denied_by_operator", False)
            return

        if kind == "checks_baseline_start":
            self._status(f"baseline: {' && '.join(payload.get('checks') or [])}")
            return

        if kind == "model_call_start":
            # Only the first read: it is the one that may queue behind a
            # model load. Later steps would repeat the name every tool call.
            if payload.get("step") == 1:
                self._status(f"waiting on {payload.get('model', '?')}")
            return

        if kind == "stuck_nudge":
            c = self._palette
            print(f"  {c.dim}stuck: nudged (run {payload.get('run', '?')}){c.reset}",
                  file=self.stream, flush=True)
            return

        if kind == "stuck":
            c = self._palette
            print(f"  {c.yellow}stuck: halted after {payload.get('run', '?')} "
                  f"repeats{c.reset}", file=self.stream, flush=True)
            return

        if kind == "truncated_response":
            c = self._palette
            print(f"  {c.yellow}truncated: reply cut at the output limit, "
                  f"retrying with max_tokens={payload.get('num_predict', '?')}{c.reset}",
                  file=self.stream, flush=True)
            return

        if kind == "audit_entry":
            # Unknown-tool path skips sentinel_verdict; pull tool from the
            # audit entry itself so we still render something.
            pending = self._pending or {
                "tool": payload.get("tool", "?"),
                "allowed": payload.get("allowed", False),
                "reason": payload.get("sentinel_reason", ""),
            }
            self._render_dispatch(pending)
            self._pending = None
            return

    def _render_dispatch(self, p: dict) -> None:
        c = self._palette
        tool = p.get("tool", "?")
        args = p.get("args", {})
        allowed = p.get("allowed", True)
        reason = p.get("reason", "")
        is_error = p.get("is_error", False)
        denied = p.get("denied_by_operator", False)

        arg_summary = _summarize_args(tool, args) if args else ""

        if not allowed:
            line = (
                f"  {c.red}⊘{c.reset} {c.bold}{tool}{c.reset}"
                f"  {c.dim}{arg_summary}{c.reset}"
                f"  {c.red}blocked: {reason}{c.reset}"
            )
        elif denied:
            line = (
                f"  {c.yellow}⊘{c.reset} {c.bold}{tool}{c.reset}"
                f"  {c.dim}{arg_summary}{c.reset}"
                f"  {c.yellow}denied by operator{c.reset}"
            )
        elif is_error:
            line = (
                f"  {c.yellow}⎿{c.reset} {c.bold}{tool}{c.reset}"
                f"  {c.dim}{arg_summary}{c.reset}"
                f"  {c.yellow}error{c.reset}"
            )
        else:
            line = (
                f"  {c.dim}⎿{c.reset} {c.bold}{tool}{c.reset}"
                f"  {c.dim}{arg_summary}{c.reset}"
            )
        print(line, file=self.stream, flush=True)

    def _render_error(self, payload: dict) -> None:
        c = self._palette
        err = payload.get("error", "unknown error")
        print(
            f"  {c.red}⚠ {err}{c.reset}",
            file=self.stream,
            flush=True,
        )


# ── Diff rendering + interactive confirm ────────────────────────────


_DIFF_TRUNCATE_LINES = 60  # show full diff above this with `d`


def render_diff(unified: str, *, stream=sys.stderr, max_lines: int | None = _DIFF_TRUNCATE_LINES) -> None:
    """Print a unified diff with ANSI coloring (green +, red -, cyan @@).

    Truncates to ``max_lines`` (showing a "[N more lines truncated]" footer)
    unless ``max_lines`` is None.
    """
    c = palette(stream)
    lines = unified.splitlines()
    truncated = False
    if max_lines is not None and len(lines) > max_lines:
        head = max_lines // 2
        tail = max_lines - head - 1
        omitted = len(lines) - head - tail
        body_lines = (
            lines[:head]
            + [f"... [{omitted} more lines — press 'd' for full diff] ..."]
            + lines[-tail:]
        )
        truncated = True
    else:
        body_lines = lines

    for line in body_lines:
        if line.startswith("+++") or line.startswith("---"):
            print(f"  {c.bold}{line}{c.reset}", file=stream)
        elif line.startswith("@@"):
            print(f"  {c.cyan}{line}{c.reset}", file=stream)
        elif line.startswith("+"):
            print(f"  {c.green}{line}{c.reset}", file=stream)
        elif line.startswith("-"):
            print(f"  {c.red}{line}{c.reset}", file=stream)
        elif line.startswith("..."):
            print(f"  {c.dim}{line}{c.reset}", file=stream)
        else:
            print(f"  {line}", file=stream)
    if truncated:
        # Trailing blank line for separation; no flush — caller flushes prompt.
        print(file=stream)


def confirm_paste_echo(
    user_prompt: str,
    tool_name: str,
    args: dict,
    *,
    stream=sys.stderr,
) -> bool:
    """Hard y/N gate when the model wants to run a Bash command that echoes
    the user's prompt. Default N — bare Enter denies.

    Auto-denies when stdin is not a TTY, matching ``confirm_diff`` semantics.
    """
    c = palette(stream)
    command = args.get("command", "") if isinstance(args, dict) else str(args)
    print(file=stream)
    print(
        f"  {c.bold}{c.yellow}!! paste-echo gate: model wants to run your "
        f"prompt as a shell command{c.reset}",
        file=stream,
    )
    print(f"  {c.dim}prompt:{c.reset}  {user_prompt[:240]}", file=stream)
    print(f"  {c.dim}command:{c.reset} {command[:240]}", file=stream)

    if not sys.stdin.isatty():
        print(
            f"  {c.yellow}stdin not a tty — denying (paste-echo){c.reset}",
            file=stream,
            flush=True,
        )
        return False

    while True:
        try:
            choice = input(
                "  Run this as a shell command? [y/N] "
            ).strip().lower()
        except EOFError:
            print(file=stream)
            return False
        if choice in ("", "n", "no"):
            return False
        if choice in ("y", "yes"):
            return True
        print(
            f"  {c.dim}answer y or n (default){c.reset}",
            file=stream,
            flush=True,
        )


def confirm_command(tool: str, args: dict, reason: str, *, stream=sys.stderr) -> bool:
    """y/N gate for a call the autonomy ladder wants approved; the command is
    shown in full. Default N; auto-denies when stdin is not a TTY."""
    c = palette(stream)
    key = _TOOL_ARG_KEY.get(tool)
    shown = str(args.get(key)) if isinstance(args, dict) and key in args else repr(args)
    print(file=stream)
    print(
        f"  {c.bold}{c.yellow}?? {tool} needs approval{c.reset}  {c.dim}{reason}{c.reset}",
        file=stream,
    )
    for line in shown.splitlines() or [""]:
        print(f"  {c.cyan}|{c.reset} {line}", file=stream)

    if not sys.stdin.isatty():
        print(
            f"  {c.yellow}stdin not a tty, denying{c.reset}",
            file=stream,
            flush=True,
        )
        return False

    while True:
        try:
            choice = input("  allow this call? [y/N] ").strip().lower()
        except EOFError:
            print(file=stream)
            return False
        if choice in ("", "n", "no"):
            return False
        if choice in ("y", "yes"):
            return True
        print(
            f"  {c.dim}answer y or n (default){c.reset}",
            file=stream,
            flush=True,
        )


def confirm_diff(plan: WritePlan, *, stream=sys.stderr) -> bool:
    """Print the diff and prompt for y/N/d. Returns True on apply, False on deny.

    Falls back to auto-deny when stdin is not a TTY (a no-op safety net — the
    REPL caller already guards interactivity, this just protects against being
    accidentally wired up in a piped context).
    """
    c = palette(stream)
    print(file=stream)
    print(f"  {c.bold}{plan.summary}{c.reset}", file=stream)
    if plan.unified_diff:
        render_diff(plan.unified_diff, stream=stream)
    else:
        print(f"  {c.dim}(no textual diff — empty file or binary){c.reset}", file=stream)

    # Sentinel-like prompt; default N on bare Enter.
    if not sys.stdin.isatty():
        print(
            f"  {c.yellow}stdin not a tty, denying write (print mode with "
            f"--autonomy low applies edits without a prompt){c.reset}",
            file=stream,
            flush=True,
        )
        return False

    while True:
        try:
            choice = input("  apply this change? [y/N/d=full diff] ").strip().lower()
        except EOFError:
            print(file=stream)
            return False
        if choice in ("", "n", "no"):
            return False
        if choice in ("y", "yes"):
            return True
        if choice in ("d", "diff", "full"):
            print(file=stream)
            render_diff(plan.unified_diff, stream=stream, max_lines=None)
            continue
        print(
            f"  {c.dim}answer y, n (default), or d{c.reset}",
            file=stream,
            flush=True,
        )
