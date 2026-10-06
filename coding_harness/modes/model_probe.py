"""Does a model actually call tools? One forced call, cached per model digest.

Exports ``probe``, ``cached``, ``start`` and ``probing``. Ollama lists
``tools`` for nearly every model, including ones that answer "I can't modify
files" and never call anything. The probe asks for one exact tool call; the
verdict is cached in <meta>/model-probes.json keyed by model and digest, so a
re-pulled model is probed again. A pass is necessary, not sufficient: a model
can make one forced call and still flounder on a real task.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from coding_harness.core import paths

TIMEOUT_S = 90.0
_TOOL = {"type": "function", "function": {
    "name": "report",
    "description": "Report a number.",
    "parameters": {"type": "object", "properties": {"value": {"type": "integer"}},
                   "required": ["value"]},
}}
_PROMPT = "Call the report tool with value 42. Do not answer in text."
_LOCK = threading.Lock()
_IN_FLIGHT: set[str] = set()  # GLOBAL-STATE: probes running in this process


def _cache_path():
    return paths.meta_dir() / "model-probes.json"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _key(model: str, digest: str) -> str:
    return f"{model}@{digest}"


def cached(model: str, digest: str) -> bool | None:
    """The cached verdict, or None when this model and digest were never probed."""
    entry = _load().get(_key(model, digest))
    return entry.get("agent") if isinstance(entry, dict) else None


def _passed(msg: dict[str, Any]) -> bool:
    """True when the reply calls report with value 42."""
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                continue
        if fn.get("name") == "report" and isinstance(args, dict) and args.get("value") in (42, "42"):
            return True
    return False


def probe(model: str, digest: str) -> dict[str, Any]:
    """Run the forced-call probe and record the verdict."""
    from coding_harness.models import ollama

    started = time.monotonic()
    try:
        msg = ollama.chat(model=model, messages=[{"role": "user", "content": _PROMPT}],
                          tools=[_TOOL], temperature=0.0, max_tokens=128, timeout=TIMEOUT_S)
        result = {"agent": _passed(msg), "detail": (msg.get("content") or "")[:200]}
    except Exception as e:  # noqa: BLE001 — a probe that errors is recorded, not raised
        result = {"agent": False, "detail": f"{type(e).__name__}: {e}"[:200]}
    result["ms"] = int((time.monotonic() - started) * 1000)
    with _LOCK:
        data = _load()
        data[_key(model, digest)] = result
        _cache_path().parent.mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(json.dumps(data, indent=1), encoding="utf-8")
    return result


def probing(model: str) -> bool:
    with _LOCK:
        return model in _IN_FLIGHT


def start(model: str, digest: str) -> bool:
    """Probe in the background; False when one is already running for this model."""
    with _LOCK:
        if model in _IN_FLIGHT:
            return False
        _IN_FLIGHT.add(model)

    def _run() -> None:
        try:
            probe(model, digest)
        finally:
            with _LOCK:
                _IN_FLIGHT.discard(model)

    threading.Thread(target=_run, daemon=True, name=f"probe-{model}").start()
    return True
