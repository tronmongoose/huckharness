#!/usr/bin/env python3
"""Ollama serving bench (P0-0): where do the seconds of a model step go?

Hits Ollama's native endpoints directly (no harness in the loop) and records
cold load, prefill and decode tok/s on a ~6k-token prompt, prefix-cache reuse,
a long generation, and a two-caller queue probe. One JSON lands in
eval/results/ with every raw number plus the derived rates.

Exports: filler_prompt, tok_per_s, queue_wait, server_gap, derive, bench_model, main.

Usage: python eval/bench_serve.py [--model TAG ...] [--url URL] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
DEFAULT_MODEL = "mistral-small3.2:latest"
HTTP_TIMEOUT_S = 600
FILLER_LINES = 270
FILLER_WORDS = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
NS = 1_000_000_000


def _http(url: str, payload: dict | None = None) -> dict:
    """One JSON round-trip. POST when a payload is given, else GET."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = Request(url, data=data, headers={"Content-Type": "application/json"},
                  method="POST" if data is not None else "GET")
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except (URLError, TimeoutError, OSError) as e:
        raise RuntimeError(f"transport: {e}") from e


def _measure(fn, *args) -> Any:
    """Run one measurement; a failure becomes an error record, never a raise."""
    try:
        return fn(*args)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def filler_prompt(n_lines: int = FILLER_LINES) -> str:
    """Deterministic numbered filler, about 22 Mistral tokens per line."""
    lines = [f"{i:05d} {FILLER_WORDS}" for i in range(n_lines)]
    return ("Read the numbered lines below, then reply with the word DONE.\n\n"
            + "\n".join(lines))


def tok_per_s(count: int | None, duration_ns: int | None) -> float | None:
    """Tokens per second from Ollama's count + nanosecond duration pair."""
    if not count or not duration_ns:
        return None
    return round(count / (duration_ns / NS), 2)


def queue_wait(wall_s: float, total_duration_ns: int | None) -> float | None:
    """Seconds the request spent outside the server's own accounting."""
    if total_duration_ns is None:
        return None
    return round(wall_s - total_duration_ns / NS, 3)


def server_gap(resp: dict) -> float | None:
    """Server time not spent loading, prefilling, or decoding: in-server queueing."""
    total = resp.get("total_duration")
    if total is None:
        return None
    busy = sum(resp.get(k) or 0 for k in ("load_duration", "prompt_eval_duration", "eval_duration"))
    return round((total - busy) / NS, 3)


def derive(resp: dict, wall_s: float) -> dict:
    """Raw Ollama timing fields plus prefill/decode tok/s and the two wait metrics."""
    keys = ("load_duration", "prompt_eval_count", "prompt_eval_duration",
            "eval_count", "eval_duration", "total_duration")
    out = {k: resp.get(k) for k in keys}
    out["wall_s"] = round(wall_s, 3)
    out["total_s"] = round((resp.get("total_duration") or 0) / NS, 3)
    out["load_s"] = round((resp.get("load_duration") or 0) / NS, 3)
    out["prefill_s"] = round((resp.get("prompt_eval_duration") or 0) / NS, 3)
    out["prefill_tps"] = tok_per_s(out["prompt_eval_count"], out["prompt_eval_duration"])
    out["decode_tps"] = tok_per_s(out["eval_count"], out["eval_duration"])
    out["queue_wait_s"] = queue_wait(wall_s, resp.get("total_duration"))
    out["server_gap_s"] = server_gap(resp)
    out["done_reason"] = resp.get("done_reason")
    return out


def _chat(url: str, model: str, prompt: str, num_predict: int) -> dict:
    """One non-streaming /api/chat call, timed on the client side."""
    payload = {
        "model": model, "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "options": {"num_predict": num_predict, "temperature": 0},
    }
    t0 = time.monotonic()
    resp = _http(f"{url}/api/chat", payload)
    return derive(resp, time.monotonic() - t0)


def _ps_rows(url: str) -> list[dict]:
    """Loaded-model rows from /api/ps, trimmed to the fields that matter."""
    rows = _http(f"{url}/api/ps").get("models") or []
    keep = ("name", "size", "size_vram", "context_length", "expires_at")
    return [{k: r.get(k) for k in keep} for r in rows]


def bench_cold(url: str, model: str) -> dict:
    """Unload if resident, then time a minimal call so load_duration is real."""
    was_loaded = any(r.get("name") == model for r in _ps_rows(url))
    if was_loaded:
        _http(f"{url}/api/generate", {"model": model, "keep_alive": 0})
        time.sleep(2)
    out = _chat(url, model, "Reply with OK.", 4)
    out["was_loaded_before"] = was_loaded
    return out


def bench_cache(url: str, model: str, prompt: str, cold: dict) -> dict:
    """Same 6k prompt again; Ollama 0.20 keeps prompt_eval_count at the full
    prompt size on a hit, so the prefill duration ratio is the reliable signal."""
    again = _chat(url, model, prompt, 256)
    a, b = cold.get("prompt_eval_count"), again.get("prompt_eval_count")
    delta = (a - b) if isinstance(a, int) and isinstance(b, int) else None
    ratio = None
    if cold.get("prefill_s"):
        ratio = round(again["prefill_s"] / cold["prefill_s"], 4)
    reused = bool(delta and delta > 0) or bool(ratio is not None and ratio < 0.1)
    return {"again": again, "prompt_eval_count_delta": delta,
            "prefill_duration_ratio": ratio, "prefix_reused": reused}


def bench_long(url: str, model: str) -> dict:
    """Write-tool-shaped call: ask for a long listing, allow 1500 tokens."""
    prompt = ("List 300 plausible file paths for a large Python web application, "
              "one per line, with a one-sentence description after each path. "
              "Do not stop early.")
    return _chat(url, model, prompt, 1500)


def bench_queue(url: str, model: str, prompt: str) -> dict:
    """Two concurrent 6k-prompt calls; queue_wait exposes server serialization."""
    results: dict[str, dict] = {}

    def worker(tag: str) -> None:
        results[tag] = _measure(_chat, url, model, prompt, 256)

    t0 = time.monotonic()
    threads = [threading.Thread(target=worker, args=(t,)) for t in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(HTTP_TIMEOUT_S + 5)
    results["both_wall_s"] = round(time.monotonic() - t0, 3)
    calls = [r for r in (results.get("a"), results.get("b")) if isinstance(r, dict)]
    for key in ("queue_wait_s", "server_gap_s"):
        vals = [r.get(key) for r in calls if r.get(key) is not None]
        results[f"max_{key}"] = max(vals or [0.0])
    return results


def bench_model(url: str, model: str) -> dict:
    """All measurements for one model tag, in dependency order."""
    prompt = filler_prompt()
    out: dict[str, Any] = {"model": model, "filler_chars": len(prompt)}
    out["show"] = _measure(lambda: _show(url, model))
    out["cold_load"] = _measure(bench_cold, url, model)
    out["prefill_decode"] = _measure(_chat, url, model, prompt, 256)
    out["cache_probe"] = _measure(bench_cache, url, model, prompt, out["prefill_decode"])
    out["long_generation"] = _measure(bench_long, url, model)
    out["queue_probe"] = _measure(bench_queue, url, model, prompt)
    out["ps_after"] = _measure(_ps_rows, url)
    return out


def _show(url: str, model: str) -> dict:
    """Model card fields from /api/show that bear on latency."""
    info = _http(f"{url}/api/show", {"model": model})
    details = info.get("details") or {}
    minfo = info.get("model_info") or {}
    ctx = {k: v for k, v in minfo.items() if k.endswith(".context_length")}
    return {"family": details.get("family"), "parameter_size": details.get("parameter_size"),
            "quantization": details.get("quantization_level"), "context_length": ctx}


def fleet_processes() -> list[str]:
    """Harness serve workers currently running (they share the Ollama server)."""
    try:
        proc = subprocess.run(["pgrep", "-fl", "coding_harness serve"],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return [ln for ln in proc.stdout.splitlines() if ln.strip()]


def _pick(d: Any, *path: str) -> Any:
    """Walk nested measurement dicts; any error record along the way reads as err."""
    for key in path:
        if not isinstance(d, dict) or "error" in d:
            return "err"
        d = d.get(key)
    return "err" if isinstance(d, dict) and "error" in d else d


def _table(m: dict) -> str:
    """Compact stdout summary for one model."""
    rows = [
        ("cold load s", _pick(m, "cold_load", "load_s")),
        ("prefill tok/s", _pick(m, "prefill_decode", "prefill_tps")),
        ("decode tok/s", _pick(m, "prefill_decode", "decode_tps")),
        ("6k prompt wall s", _pick(m, "prefill_decode", "wall_s")),
        ("cache prompt_eval delta", _pick(m, "cache_probe", "prompt_eval_count_delta")),
        ("cache prefill ratio", _pick(m, "cache_probe", "prefill_duration_ratio")),
        ("long gen tok/s", _pick(m, "long_generation", "decode_tps")),
        ("long gen tokens", _pick(m, "long_generation", "eval_count")),
        ("long gen wall s", _pick(m, "long_generation", "wall_s")),
        ("queue wait a / b s", f"{_pick(m, 'queue_probe', 'a', 'queue_wait_s')} / "
                               f"{_pick(m, 'queue_probe', 'b', 'queue_wait_s')}"),
        ("server gap a / b s", f"{_pick(m, 'queue_probe', 'a', 'server_gap_s')} / "
                               f"{_pick(m, 'queue_probe', 'b', 'server_gap_s')}"),
        ("queue both wall s", _pick(m, "queue_probe", "both_wall_s")),
    ]
    width = max(len(r[0]) for r in rows)
    return "\n".join(f"  {k:<{width}}  {v}" for k, v in rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="Ollama serving bench")
    ap.add_argument("--model", action="append", default=[], help="repeatable")
    ap.add_argument("--url", default=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    models = args.model or [DEFAULT_MODEL]
    url = args.url.rstrip("/")

    version = _measure(lambda: _http(f"{url}/api/version").get("version"))
    version_tag = version if isinstance(version, str) else "unknown"
    fleet = fleet_processes()
    report: dict[str, Any] = {
        "date": date.today().isoformat(), "ollama_version": version, "url": url,
        "fleet_active": bool(fleet), "fleet_processes": fleet,
        "ps_before": _measure(_ps_rows, url), "models": {},
    }
    print(f"ollama {version_tag} at {url}; fleet_active={bool(fleet)}")
    for model in models:
        print(f"\n{model}")
        report["models"][model] = bench_model(url, model)
        print(_table(report["models"][model]))

    RESULTS_DIR.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else RESULTS_DIR / f"serve-{version_tag}-{report['date']}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
