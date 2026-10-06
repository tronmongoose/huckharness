"""Ollama tool-calling client.

Uses the OpenAI-compatible ``/v1/chat/completions`` endpoint with the ``tools``
parameter. The model emits ``tool_calls`` which the harness session loop
dispatches via ``ToolRegistry``.

For M1 we target ``mistral-small3.2:latest`` (Mistral AI). This aligns with
the project's "Coding" default per sl-h5h.3 / commit 403c40d, which ran a
5-task code-generation rubric and scored:

- ``mistral-small3.2:latest`` — 18/18, 9.4s avg latency. Project default.
- ``devstral:latest`` — 17/18, 23.5s avg latency. Was "Coding (legacy)" /
  fallback; dropped from local inventory 2026-07-02 (model refresh, sl-37d4).
- ``gemma4:26b`` (Google) — 14/18, 39.9s avg. CoT eats output budget.
  Additionally emits zero tool calls via Ollama's OpenAI-compat endpoint, so
  even if its scores improved it would not work as a tool-driving harness
  default until the Ollama bridge handles its output shape.

Hermes 3 (``hermes3:8b``, Nous Research) is intentionally absent from the
harness rotation. It's a function-calling fine-tune of Llama, not a coding
model, and an early head-to-head on this harness's tool-loop sanity test
showed it ignored the system prompt and hallucinated paths.

The harness also runs its own tool-loop sanity test (recursively find a
file, identify a function in it) before any default change. Both
mistral-small3.2 and devstral pass that test in 3 turns under the current
system prompt (cwd injection + tool-selection rules + "do not announce
future tool calls — execute them"). Mistral-small3.2 was picked over
devstral for the harness because the wider eval favors it on every axis
and aligning the defaults eliminates fragmentation.

Banned-origin enforcement happens here at call time (README, Model
policy). There's also a CI test (``tests/test_banned_models.py``) that asserts
the same blocklist.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from http.client import HTTPException
from typing import Any

from coding_harness.models import transport

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")

# Maintainer policy, see README "Model policy". Lowercase for case-insensitive match.
BANNED_MODEL_PREFIXES: tuple[str, ...] = (
    "qwen", "qwq", "deepseek", "yi", "yi:", "yi-", "baichuan", "chatglm", "glm-", "glm4",
    "internlm", "internvl", "minimax", "kimi", "moonshot", "hunyuan", "bytedance", "doubao",
    "seed-", "ernie", "baidu", "zhipu", "stepfun", "skywork", "inclusionai",
    "ling-", "cogvlm", "cogagent", "marco-o1", "pangu", "telechat", "xverse", "aquila",
)

_ALLOWED_HINT = "Mistral / Devstral / Gemma / Granite / Nemotron / Llama / Phi / gpt-oss"
_BOUNDARY = r"[/:\-_.]"


def _slug_pattern(slug: str) -> str:
    """Substring match for long plain slugs; a short slug must fill a whole name segment."""
    esc = re.escape(slug)
    if len(slug) >= 4 and slug[-1] not in ":-":
        return f"({esc})"
    if slug[-1] in ":-":
        return f"((?:^|{_BOUNDARY}){esc})"
    return f"((?:^|{_BOUNDARY}){esc}(?:$|{_BOUNDARY}))"


_BANNED_RE = re.compile("|".join(_slug_pattern(s) for s in BANNED_MODEL_PREFIXES))


class BannedModelError(ValueError):
    """Raised when a caller asks for a Chinese-origin model."""


def _banned_slug(name: str) -> str | None:
    """Return the banned slug found in ``name`` (case-insensitive), or None."""
    m = _BANNED_RE.search(name.lower())
    if m is None:
        return None
    return BANNED_MODEL_PREFIXES[m.lastindex - 1]


def assert_model_allowed(model: str) -> None:
    """Raise BannedModelError if ``model`` names a Chinese-origin family."""
    slug = _banned_slug(model)
    if slug is not None:
        raise BannedModelError(
            f"refusing to use model {model!r}: slug {slug!r} is banned "
            f"by this project's model-origin policy (no Chinese-origin LLMs). "
            f"Use {_ALLOWED_HINT} instead."
        )


def _wire(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """History as the server should see it: harness-private ``_`` keys dropped."""
    return [{k: v for k, v in m.items() if not k.startswith("_")} for m in messages]


def chat(
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.2,
    max_tokens: int = 2048,
    top_p: float | None = None,
    timeout: float | None = None,
    on_delta: Callable[[str], None] | None = None,
    response_format: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One round-trip to Ollama. Returns the parsed assistant ``message`` dict.

    The returned dict has the OpenAI shape: ``{"role": "assistant", "content":
    str | None, "tool_calls": [...]?}``. The session loop appends it directly
    to the running message list and inspects ``tool_calls``.

    When ``on_delta`` is supplied, the request streams and ``on_delta`` is
    called with each assistant-text token as it arrives. The assembled final
    message (content + tool_calls + ``_usage``) is still returned unchanged, so
    non-streaming callers (print/repl/nightshift) are unaffected.

    ``timeout`` is the socket read timeout in seconds; ``None`` resolves to
    ``transport.read_timeout_s()`` at call time so HARNESS_READ_TIMEOUT is
    honoured. Callers with a wall-clock deadline pass something smaller.

    ``response_format`` is forwarded verbatim when set (Ollama's /v1 honours
    ``{"type": "json_object"}``); omitted from the payload otherwise. The same
    goes for ``top_p``: the profile layer (models/profile.py) supplies it per
    family, and ``None`` keeps the body identical to the pre-profile one.

    Raises on transport / HTTP / parse errors so the loop can decide whether
    to retry, fall back, or surface the error to the operator. M1 surfaces.
    """
    assert_model_allowed(model)
    if timeout is None:
        timeout = transport.read_timeout_s()

    payload: dict[str, Any] = {
        "model": model,
        "messages": _wire(messages),
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": on_delta is not None,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    if top_p is not None:
        payload["top_p"] = top_p
    if response_format is not None:
        payload["response_format"] = response_format
    if on_delta is not None:
        # Ask the OpenAI-compat endpoint to emit a final usage chunk so the
        # streaming path keeps the token telemetry the non-streaming path has
        # (nightshift's circuit breaker reads it — sl-zvi3).
        payload["stream_options"] = {"include_usage": True}

    url = f"{OLLAMA_URL}/v1/chat/completions"
    body = json.dumps(payload).encode("utf-8")
    if on_delta is not None:
        return _chat_streaming(url, body, timeout, on_delta)
    return transport.with_retries(lambda: _chat_once(url, body, timeout))


def _chat_once(url: str, body: bytes, timeout: float) -> dict[str, Any]:
    """One non-streaming request; raises transport.EmptyResponse so it can be retried."""
    with transport.open_stream(url, body, read_timeout=timeout) as resp:
        raw = resp.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"ollama returned non-JSON: {e}") from e

    choices = data.get("choices") or []
    if not choices:
        raise transport.EmptyResponse(f"ollama returned no choices: {data}")
    msg = choices[0].get("message")
    if not isinstance(msg, dict):
        raise RuntimeError(f"ollama choice has no message dict: {choices[0]}")

    # Normalize: ensure content key exists even if model only emitted tool_calls.
    msg.setdefault("content", "")
    msg["_usage"] = _usage(data.get("usage") or {}, choices[0].get("finish_reason"))
    return msg


def _usage(usage: dict[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
    """Token telemetry for sl-nmc.4 from Ollama's OpenAI-compat ``usage`` block.

    ``thinking_tokens`` is None here — Ollama does not expose CoT separately
    even for models that do internal reasoning (Gemma 4). ``finish_reason``
    rides along so the session can tell a reply cut at max_tokens from a
    complete one without the message shape leaving the OpenAI form.
    """
    return {
        "tokens_in": int(usage.get("prompt_tokens") or 0),
        "tokens_out": int(usage.get("completion_tokens") or 0),
        "thinking_tokens": None,
        "finish_reason": finish_reason,
    }


def _chat_streaming(
    url: str, body: bytes, timeout: float, on_delta: Callable[[str], None]
) -> dict[str, Any]:
    """Consume an OpenAI-compat SSE stream, assembling the final message.

    Text tokens are forwarded to ``on_delta`` as they arrive. Tool-call
    fragments are accumulated by index (the ``arguments`` string streams in
    pieces). ``on_delta`` may raise to cooperatively cancel the turn — the
    exception propagates to the caller, which the session loop catches.

    Retries cover only the request/headers phase: once bytes have been
    forwarded to ``on_delta`` a replay would duplicate them.
    """
    content_parts: list[str] = []
    # index -> {"id", "type", "function": {"name", "arguments"}}
    tool_acc: dict[int, dict[str, Any]] = {}
    resp = transport.with_retries(
        lambda: transport.open_stream(url, body, read_timeout=timeout)
    )
    try:
        with resp:
            usage, finish_reason = _consume_sse(resp, on_delta, content_parts, tool_acc)
    except (OSError, HTTPException) as e:
        raise RuntimeError(f"ollama transport error: {e}") from e

    msg: dict[str, Any] = {"role": "assistant", "content": "".join(content_parts)}
    if tool_acc:
        msg["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
    msg["_usage"] = _usage(usage, finish_reason)
    return msg


def _consume_sse(
    resp: Any,
    on_delta: Callable[[str], None],
    content_parts: list[str],
    tool_acc: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any], str | None]:
    """Fold SSE chunks into ``content_parts`` / ``tool_acc``; return (usage, last finish_reason)."""
    usage: dict[str, Any] = {}
    finish_reason: str | None = None
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data:"):
            continue
        data_str = line[len("data:"):].strip()
        if data_str == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                content_parts.append(text)
                on_delta(text)
            for tc in delta.get("tool_calls") or []:
                _merge_tool_call(tool_acc, tc)
    return usage, finish_reason


def _merge_tool_call(acc: dict[int, dict[str, Any]], delta_tc: dict[str, Any]) -> None:
    """Fold one streamed tool-call delta into the accumulator by index."""
    idx = delta_tc.get("index", 0)
    slot = acc.setdefault(idx, {"id": "", "type": "function",
                                "function": {"name": "", "arguments": ""}})
    if delta_tc.get("id"):
        slot["id"] = delta_tc["id"]
    if delta_tc.get("type"):
        slot["type"] = delta_tc["type"]
    fn = delta_tc.get("function") or {}
    if fn.get("name"):
        slot["function"]["name"] += fn["name"]
    if fn.get("arguments"):
        slot["function"]["arguments"] += fn["arguments"]
