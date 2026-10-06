"""Per-model sampling profiles (P1-1).

One frozen ``ModelProfile`` per model family carries the sampling settings a
run used, so a transcript and an eval fingerprint can say which knobs were
on. The /v1 endpoint accepts only temperature, max_tokens and top_p, so
``num_ctx``, ``top_k``, ``think`` and ``keep_alive`` are recorded targets
until the native /api/chat path (P2-3) and Modelfiles (P2-4) consume them.
``num_ctx`` feeds the token budget (P1-5), not the request.

Operator prerequisite: run Ollama with ``OLLAMA_NUM_PARALLEL=1``. Each
parallel slot allocates its own KV cache at ``num_ctx``, so a 32k target
multiplies by the slot count otherwise.

Exports: ModelProfile, FAMILIES, resolve_profile, profile_hash, sampling_kwargs.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, replace
from typing import Any

MISTRAL_TEMPLATE_QUIRK = "le (len (slice $.Messages $index)) 2"


@dataclass(frozen=True)
class ModelProfile:
    """Sampling and serving settings for one model family."""

    family: str
    temperature: float = 0.2
    top_p: float | None = None
    top_k: int | None = None
    num_ctx: int = 32768
    num_predict: int = 4096
    think: bool = False
    keep_alive: str | None = None
    stop: tuple[str, ...] = ()
    tool_aliases: tuple[tuple[str, str], ...] = ()
    compactor: str | None = None
    template_lint: str | None = None
    supports_format: bool = False


_MISTRAL = dict(temperature=0.15, top_p=0.95, top_k=40, num_ctx=32768,
                num_predict=4096, think=False, template_lint=MISTRAL_TEMPLATE_QUIRK)
_GRANITE = dict(temperature=0.1, num_ctx=32768, think=False, supports_format=True)

# Match order matters: "devstral" must win before any looser substring.
FAMILIES: tuple[ModelProfile, ...] = (
    ModelProfile("devstral", **_MISTRAL),
    ModelProfile("mistral-small", **_MISTRAL),
    ModelProfile("granite4.2", **_GRANITE),
    ModelProfile("granite4.1", **_GRANITE),
    ModelProfile("nemotron-3-nano", temperature=0.2, num_ctx=32768, think=False),
    ModelProfile("laguna", temperature=1.0, top_p=1.0, top_k=20, think=True),
    ModelProfile("gpt-oss", temperature=0.2),
    ModelProfile("gemma", temperature=0.2),
)
GENERIC = ModelProfile("generic", temperature=0.2, num_ctx=32768, num_predict=2048)


def _family_key(model: str) -> str:
    """Lowercase model name with any ``hf.co/org/`` prefix and ``:tag`` removed."""
    name = model.lower().rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def _base_profile(model: str) -> ModelProfile:
    """First family whose name is a prefix or substring of the model's key."""
    key = _family_key(model)
    for fam in FAMILIES:
        if fam.family in key:
            return fam
    return GENERIC


def _env_int(name: str) -> int | None:
    """Positive int from the environment, None when unset or invalid."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _env_float(name: str) -> float | None:
    """Non-negative float from the environment, None when unset or invalid."""
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _env_overrides() -> dict[str, Any]:
    """Valid HARNESS_* overrides as profile field values."""
    out: dict[str, Any] = {}
    num_ctx, num_predict = _env_int("HARNESS_NUM_CTX"), _env_int("HARNESS_NUM_PREDICT")
    temperature = _env_float("HARNESS_TEMPERATURE")
    if num_ctx is not None:
        out["num_ctx"] = num_ctx
    if num_predict is not None:
        out["num_predict"] = num_predict
    if temperature is not None:
        out["temperature"] = temperature
    think = os.environ.get("HARNESS_THINK")
    if think in ("0", "1"):
        out["think"] = think == "1"
    keep_alive = os.environ.get("HARNESS_KEEP_ALIVE")
    if keep_alive:
        out["keep_alive"] = keep_alive
    return out


def resolve_profile(model: str) -> ModelProfile:
    """Family profile for ``model`` with valid HARNESS_* environment overrides applied."""
    return replace(_base_profile(model), **_env_overrides())


def profile_hash(profile: ModelProfile) -> str:
    """First 12 hex of sha256 over the profile's sorted field dict."""
    canonical = json.dumps(asdict(profile), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def sampling_kwargs(profile: ModelProfile) -> dict[str, Any]:
    """The subset of the profile that ``ollama.chat`` sends over /v1 today."""
    return {
        "temperature": profile.temperature,
        "max_tokens": profile.num_predict,
        "top_p": profile.top_p,
    }
