"""Max-plan frontier backend via the local ``claude`` CLI — text-only.

Runs ``claude -p --output-format stream-json`` as a subprocess and adapts it
to the same ``chat(...) -> assistant message dict`` contract as the ollama
and anthropic clients. Spends Claude Max plan quota, not API credits, which
is why session.py exempts this backend from the ``generation.local_only``
knob and the ``_cost_log`` spend cap (both exist to protect API dollars).

**Text-only by construction.** The subprocess runs with ``--tools ""`` so
Claude Code cannot execute anything — no file reads, no Bash, no MCP. That
is the governance stance Erik chose for P3: frontier turns are pure
reasoning/writing; anything that drives tools stays on the governed local
model where the envelope, Sentinel, and audit chain sit in the dispatch
path. This backend therefore never returns ``tool_calls``.

**Gates run before the subprocess spawns** — same order as anthropic.py:
``assert_frontier_allowed`` (lethal trifecta) then
``assert_model_allowed`` (banned origins).
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable

from coding_harness.core.router import RouteDecision
from coding_harness.models.anthropic import assert_frontier_allowed
from coding_harness.models.ollama import assert_model_allowed

CLAUDE_BIN = "claude"
DEFAULT_CLI_MODEL = "sonnet"
DEFAULT_TIMEOUT_S = 300

# Aliases the CLI accepts directly; anything else is passed through verbatim
# (full model ids like claude-sonnet-4-6) and still checked against the
# banned-origin list.
_KNOWN_ALIASES = {"sonnet", "opus", "haiku", "fable"}


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Collapse the conversation into one prompt string.

    ``claude -p`` takes a single prompt, not a message list. For text-only
    reasoning turns a labeled transcript preserves enough context; tool
    messages are included as observations so the frontier model can reason
    over local tool results it did not itself produce.
    """
    parts: list[str] = []
    for m in messages:
        role = m.get("role", "")
        content = m.get("content") or ""
        if not content:
            continue
        if role == "system":
            parts.append(f"<system>\n{content}\n</system>")
        elif role == "user":
            parts.append(f"User: {content}")
        elif role == "assistant":
            parts.append(f"Assistant: {content}")
        elif role == "tool":
            name = m.get("name", "tool")
            parts.append(f"[{name} result]\n{content}")
    parts.append(
        "Respond directly as the assistant. You have no tools in this "
        "context; do not emit tool calls or claim to have run anything."
    )
    return "\n\n".join(parts)



def _cli_env() -> dict[str, str]:
    """The CLI subprocess env: no API key, and HOME from HARNESS_CLAUDE_HOME when set.

    The eval runs the harness under a temp HOME so the agent never sees the
    operator's skills or memory. The CLI's login still lives in the real
    home, which the runner passes in HARNESS_CLAUDE_HOME for this process only.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    claude_home = env.get("HARNESS_CLAUDE_HOME", "").strip()
    if claude_home:
        env["HOME"] = claude_home
    return env

def chat(
    *,
    model: str,
    messages: list[dict[str, Any]],
    decision: RouteDecision | None,
    tools: list[dict[str, Any]] | None = None,  # noqa: ARG001 — accepted, unused
    temperature: float = 0.2,  # noqa: ARG001 — CLI has no temperature knob
    max_tokens: int = 2048,  # noqa: ARG001 — CLI manages its own budget
    timeout: int = DEFAULT_TIMEOUT_S,
    on_delta: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One text-only frontier turn through ``claude -p``.

    Returns the standard assistant dict: ``{"role", "content",
    "tool_calls": [], "_usage": {...}}``. ``tools`` is accepted for
    signature parity with the other backends and deliberately ignored.
    Raises RuntimeError on subprocess failure or timeout so the session
    loop's existing error path handles it.
    """
    assert_frontier_allowed(decision)
    assert_model_allowed(model)

    prompt = _flatten_messages(messages)
    cli_model = model if model in _KNOWN_ALIASES else model
    cmd = [
        CLAUDE_BIN,
        "-p", prompt,
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",  # stream-json with -p requires it in current CLI versions
        "--tools", "",
        "--no-session-persistence",
        "--model", cli_model,
    ]

    # Strip API-key auth from the subprocess env so the CLI authenticates via
    # the logged-in claude.ai account (Max plan). With ANTHROPIC_API_KEY set,
    # claude -p silently bills the API key instead — the exact spend this
    # backend exists to avoid (verified live 2026-07-11: the CLI warns
    # "another auth source is set" and prefers the key).
    env = _cli_env()

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError as e:
        raise RuntimeError(
            f"claude CLI not found ({CLAUDE_BIN!r}); is Claude Code installed?"
        ) from e

    try:
        content, usage = _consume_stream(proc, on_delta)
        _, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        proc.communicate()
        raise RuntimeError(f"claude -p timed out after {timeout}s") from e
    except BaseException:
        # on_delta may raise to cancel cooperatively (interrupt). Don't leave
        # the subprocess running headless on the Max plan.
        proc.kill()
        proc.communicate()
        raise

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {(stderr or '')[:500]}"
        )
    if content is None:
        raise RuntimeError("claude -p produced no result event")

    return {
        "role": "assistant",
        "content": content,
        "tool_calls": [],
        "_usage": usage,
    }


def _consume_stream(
    proc: subprocess.Popen[str],
    on_delta: Callable[[str], None] | None,
) -> tuple[str | None, dict[str, Any]]:
    """Read stream-json lines: forward text deltas, capture the result event.

    Event shapes (claude CLI 2.x):
      {"type":"stream_event","event":{"type":"content_block_delta",
        "delta":{"type":"text_delta","text":"..."}}}       — partial text
      {"type":"assistant","message":{"content":[{"type":"text",...}]}}
                                                            — full message
      {"type":"result","result":"...","usage":{...}}       — terminal
    """
    final_text: str | None = None
    assembled: list[str] = []
    usage: dict[str, Any] = {
        "tokens_in": 0, "tokens_out": 0, "thinking_tokens": None,
    }

    assert proc.stdout is not None
    for raw in proc.stdout:
        line = raw.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("type")
        if etype == "stream_event":
            inner = ev.get("event") or {}
            if inner.get("type") == "content_block_delta":
                delta = inner.get("delta") or {}
                text = delta.get("text")
                if text:
                    assembled.append(text)
                    if on_delta is not None:
                        on_delta(text)
        elif etype == "result":
            final_text = ev.get("result")
            u = ev.get("usage") or {}
            usage["tokens_in"] = int(
                (u.get("input_tokens") or 0)
                + (u.get("cache_read_input_tokens") or 0)
                + (u.get("cache_creation_input_tokens") or 0)
            )
            usage["tokens_out"] = int(u.get("output_tokens") or 0)

    if final_text is None and assembled:
        # Result event missing (killed stream, older CLI): fall back to the
        # assembled deltas rather than losing the turn.
        final_text = "".join(assembled)
    return final_text, usage
