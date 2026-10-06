"""Anthropic Messages API tool-calling client.

Mirrors the surface of ``coding_harness/models/ollama.py``: ``chat(model=,
messages=, tools=, ...)`` returns an OpenAI-shape assistant message dict
(``{"role": "assistant", "content": str, "tool_calls": [...]}``) so the
session loop dispatches uniformly regardless of provider.

**Lethal-trifecta gate.** This client
*requires* a ``RouteDecision`` from the deployment's router and refuses to dispatch
unless the decision explicitly approves frontier. Sensitive queries —
finance, personal — are blocked at the gate and never touch the network.
The block is hard: there is no override flag, no fallback, no soft path.

Banned-origin enforcement reuses
``assert_model_allowed`` from the Ollama client so the blocklist lives in
one place.
"""
from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from coding_harness.core.router import RouteDecision
from coding_harness.models.ollama import assert_model_allowed

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_API_VERSION = "2023-06-01"


class FrontierBlockedError(RuntimeError):
    """Raised when a caller tries to dispatch frontier without route approval.

    This is the lethal-trifecta load-bearing exception. If you find yourself
    catching it to retry without the gate, stop — the architecture relies on
    this never being silently swallowed.
    """


def assert_frontier_allowed(decision: RouteDecision | None) -> None:
    """Hard gate. Refuses unless decision explicitly says frontier, non-sensitive.

    Three independent checks — all must pass:
      1. Decision provided (no implicit allow).
      2. ``sensitivity_flag`` is False.
      3. ``route == "frontier"``.

    Each guard restates the rule in its message so a confused operator can
    fix the call site without spelunking through clawrouter.
    """
    if decision is None:
        raise FrontierBlockedError(
            "Anthropic dispatch refused: no RouteDecision supplied. The "
            "lethal-trifecta gate requires an explicit clawrouter decision."
        )
    if decision.sensitivity_flag:
        raise FrontierBlockedError(
            "Anthropic dispatch refused: query flagged sensitive "
            f"({decision.sensitivity.get('reasons', [])}). "
            "Sensitive data must not leave the machine."
        )
    if decision.route != "frontier":
        raise FrontierBlockedError(
            f"Anthropic dispatch refused: route={decision.route!r}, expected "
            "'frontier'. Use the local model client when route says local."
        )


# ── Message / tool shape conversion ───────────────────────────


def _tools_openai_to_anthropic(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """OpenAI ``{type:'function', function:{...}}`` → Anthropic ``{name, input_schema}``."""
    out = []
    for t in tools:
        fn = t.get("function") if t.get("type") == "function" else t
        if not fn or "name" not in fn:
            continue
        out.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return out


def _messages_openai_to_anthropic(
    messages: list[dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Split out system + translate roles. Returns (system_text, anthropic_messages)."""
    system_text: str | None = None
    out: list[dict[str, Any]] = []

    for m in messages:
        role = m.get("role")
        if role == "system":
            # Last system wins (matches typical bridge behavior).
            system_text = m.get("content") or ""
            continue

        if role == "tool":
            # OpenAI tool result → Anthropic user message with tool_result block.
            out.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_call_id", ""),
                    "content": m.get("content", ""),
                }],
            })
            continue

        if role == "assistant" and m.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            content = m.get("content")
            if content:
                blocks.append({"type": "text", "text": content})
            for tc in m["tool_calls"]:
                fn = tc.get("function", {})
                args_raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                except json.JSONDecodeError:
                    args = {}
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            out.append({"role": "assistant", "content": blocks})
            continue

        # Plain user / assistant text.
        out.append({"role": role, "content": m.get("content", "")})

    return system_text, out


def _response_anthropic_to_openai(resp: dict[str, Any]) -> dict[str, Any]:
    """Anthropic content blocks → OpenAI ``{role, content, tool_calls?}`` shape."""
    blocks = resp.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []

    for b in blocks:
        btype = b.get("type")
        if btype == "text":
            text_parts.append(b.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": b.get("id", ""),
                "type": "function",
                "function": {
                    "name": b.get("name", ""),
                    "arguments": json.dumps(b.get("input", {})),
                },
            })

    msg: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if tool_calls:
        msg["tool_calls"] = tool_calls

    # sl-nmc.4 telemetry. Anthropic exposes input/output token counts on every
    # response. ``thinking_tokens`` is None for non-extended-thinking calls;
    # callers that opt in can populate it from a future ``thinking`` block.
    usage = resp.get("usage") or {}
    msg["_usage"] = {
        "tokens_in": int(usage.get("input_tokens") or 0),
        "tokens_out": int(usage.get("output_tokens") or 0),
        "thinking_tokens": None,
    }
    return msg


# ── Public entry point ────────────────────────────────────────


def chat(
    *,
    model: str,
    messages: list[dict[str, Any]],
    decision: RouteDecision | None,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    timeout: int = 180,
    api_key: str | None = None,
) -> dict[str, Any]:
    """One round-trip to Anthropic. Returns OpenAI-shape assistant message.

    The lethal-trifecta gate runs first — the function returns / raises
    *before* any network egress when the route disallows frontier. Tests
    rely on this ordering; do not move it.
    """
    # Gate FIRST. Network egress only after both checks pass.
    assert_frontier_allowed(decision)
    assert_model_allowed(model)

    key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set in the environment."
        )

    system_text, anth_messages = _messages_openai_to_anthropic(messages)
    payload: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": anth_messages,
    }
    if system_text:
        payload["system"] = system_text
    if tools:
        payload["tools"] = _tools_openai_to_anthropic(tools)

    req = Request(
        ANTHROPIC_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": ANTHROPIC_API_VERSION,
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace") if hasattr(e, "read") else ""
        raise RuntimeError(f"anthropic HTTP {e.code}: {body[:500]}") from e
    except (URLError, TimeoutError) as e:
        raise RuntimeError(f"anthropic transport error: {e}") from e
    except json.JSONDecodeError as e:
        raise RuntimeError(f"anthropic returned non-JSON: {e}") from e

    return _response_anthropic_to_openai(data)
