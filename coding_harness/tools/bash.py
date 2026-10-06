"""Bash tool — run a shell command, capture stdout/stderr/exit, with a timeout.

The Sentinel hook fast-path already enforces destructive-command patterns and
will refuse `rm -rf`, `git reset --hard`, etc. before this tool ever sees them.
Anything that escalates to the slow path gets LLM-reviewed.

This tool does **not** add its own pattern blocking — that responsibility lives
in the gate, and duplicating it here would just create drift. The tool is
intentionally a thin wrapper; the security comes from the registry.

The command runs in its own process group and the whole group is killed on
timeout, so a backgrounded grandchild cannot outlive the call or hold the
output pipes open. The registry hands the tool ``deadline_remaining_s`` so a
per-call timeout never runs past the turn's deadline.
"""
from __future__ import annotations

import math
import os
import re
import signal
import subprocess
from typing import Any, Callable

from coding_harness.security import env_scrub
from coding_harness.tools.base import Tool, ToolResult

DEFAULT_TIMEOUT = 120  # seconds
MAX_OUTPUT = 8_000    # chars per stream; the middle is cut, head 60% and tail 40% kept
_HEAD_SHARE = 0.6
_KILL_GRACE_S = 5.0

_BAD_FLAG = re.compile(
    r"unexpected argument|unrecognized arguments|no such option|invalid option"
    r"|unknown flag|usage:",
    re.IGNORECASE,
)


def classify_error(returncode: int, stderr: str) -> str | None:
    """Split harness-caused waste (missing tool, invented flag) from informative failures."""
    if returncode == 0:
        return None
    low = stderr.lower()
    if returncode == 127 or "command not found" in low:
        return "harness_caused:command_not_found"
    if "no rule to make target" in low:
        return "harness_caused:missing_target"
    if _BAD_FLAG.search(stderr):
        return "harness_caused:bad_flag"
    return "informative"


def _truncate(s: str, cap: int = MAX_OUTPUT) -> str:
    """Middle-out cut to ``cap`` chars: the head and the tail of a long output carry the signal."""
    if len(s) <= cap:
        return s
    marker = f"\n... [truncated {len(s) - cap} chars from the middle] ...\n"
    budget = max(0, cap - len(marker))
    head = int(budget * _HEAD_SHARE)
    return s[:head] + marker + s[len(s) - (budget - head):]


def _run_group(cmd: str, timeout: float) -> subprocess.CompletedProcess:
    """Run ``cmd`` as its own process group; on timeout kill the group and re-raise."""
    proc = subprocess.Popen(
        ["/bin/bash", "-c", cmd],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
        env=env_scrub.scrub(os.environ),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the group ``proc`` leads, then reap it; a bounded wait guards an escaped pipe holder."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.communicate(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        proc.kill()


class Bash(Tool):
    name = "Bash"
    category = "execute"
    description = (
        "Run a shell command and return stdout, stderr, and exit code. "
        "Commands run via /bin/bash -c with a default 120s timeout. "
        "Destructive commands (rm -rf, git reset --hard, force push, etc.) "
        "are blocked by the Sentinel gate before the tool runs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The shell command to run.",
            },
            "timeout": {
                "type": "integer",
                "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT}, max 600).",
                "default": DEFAULT_TIMEOUT,
            },
        },
        "required": ["command"],
    }
    # Seconds left on the owning turn's deadline; the registry sets it on
    # register(). None (or inf) leaves the per-call timeout as requested.
    deadline_remaining_s: Callable[[], float] | None = None

    def _timeout_s(self, requested: Any) -> int:
        """The clamped per-call timeout: 1..600 s, never past the turn deadline."""
        timeout = max(1, min(600, int(requested or DEFAULT_TIMEOUT)))
        if self.deadline_remaining_s is None:
            return timeout
        remaining = self.deadline_remaining_s()
        if remaining < timeout:
            timeout = max(1, int(math.ceil(remaining)))
        return timeout

    def run(self, args: dict[str, Any]) -> ToolResult:
        cmd = args.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return ToolResult(content="error: command required (non-empty string)", is_error=True)

        timeout = self._timeout_s(args.get("timeout"))

        try:
            proc = _run_group(cmd, timeout)
        except subprocess.TimeoutExpired:
            return ToolResult(
                content=f"error: command timed out after {timeout}s\n$ {cmd}",
                is_error=True,
                metadata={"error_class": "harness_caused:timeout"},
            )
        except OSError as e:
            return ToolResult(content=f"error: could not exec bash: {e}", is_error=True)

        out = _truncate(proc.stdout)
        err = _truncate(proc.stderr)
        body = f"$ {cmd}\nexit: {proc.returncode}\n--- stdout ---\n{out}"
        if err:
            body += f"\n--- stderr ---\n{err}"

        metadata: dict[str, Any] = {"exit_code": proc.returncode}
        error_class = classify_error(proc.returncode, proc.stderr)
        if error_class is not None:
            metadata["error_class"] = error_class
        return ToolResult(
            content=body,
            is_error=proc.returncode != 0,
            metadata=metadata,
        )
