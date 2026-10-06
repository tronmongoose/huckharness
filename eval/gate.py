#!/usr/bin/env python3
"""Eval regression gate: harness changes ship only on eval pass.

Compares a fresh eval run against the pinned baseline in
``eval/results/baseline.json`` and exits nonzero on regression. This is the
merge gate for any change that touches the harness itself: run it before
pushing a harness-targeting diff (``make eval-gate``).

Verdict rules, in order:
  1. FAIL when a task pinned as must_pass (it passed every repeat at pin time)
     is unresolved in the new run. A two-task regression hides inside the
     aggregate slack; this rule catches it per task.
  2. FAIL when resolved-count < baseline resolved - slack. Slack exists because
     the local model is noisy run-to-run (SOTA report S34). Slack is pinned in
     the baseline, not a flag, so a caller cannot quietly loosen the gate.

Before running, the live fingerprint (eval/fingerprint.py) must match the pin
on model digest, Ollama version and sampling-profile hash, or the gate exits
3: a different model, or the same model sampled differently, is a different
baseline, not a regression. The harness sha is recorded but never
compared; it is what changes between runs. ``--allow-fingerprint-mismatch``
overrides for a deliberate cross-model look.

Re-pinning (``--pin``) is deliberate: it runs the suite and overwrites the
baseline from the result. Only pin from a state you trust. Schema 1 baselines
(no per_task, no fingerprint) still load until the next pin.

Exports: load_baseline, verdict, fingerprint_mismatch, pin, main.

Usage: python eval/gate.py [--repeats N] [--task ID ...] [--json] [--pin]
                           [--allow-fingerprint-mismatch] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from run import RESULTS_DIR, run_eval, run_meta, write_report

HERE = Path(__file__).resolve().parent
BASELINE_PATH = HERE / "results" / "baseline.json"
SCHEMA = 2
INTERIM_NOTE = "throwaway pin after phase 0; re-pinned by P2-1"
COMPARED_FIELDS = ("model_digest", "ollama_version", "profile_hash")
EXIT_FINGERPRINT = 3


def load_baseline() -> dict:
    """Pinned baseline. Missing/malformed is a hard error, never a pass.

    A schema 1 file reads as schema 2 with no per-task pins and no fingerprint.
    """
    try:
        b = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SystemExit(f"gate: no usable baseline at {BASELINE_PATH}: {e}") from e
    for key in ("resolved", "tasks", "slack"):
        if not isinstance(b.get(key), int):
            raise SystemExit(f"gate: baseline missing int field {key!r}")
    b.setdefault("schema", 1)
    b.setdefault("per_task", {})
    b.setdefault("fingerprint", None)
    return b


def verdict(rows: list[dict], baseline: dict) -> dict:
    """Pure decision: per-task must_pass first, then the aggregate floor."""
    resolved = sum(1 for r in rows if r["resolved"])
    floor = baseline["resolved"] - baseline["slack"]
    per_task = baseline.get("per_task") or {}
    must_pass_failed = sorted(
        r["id"] for r in rows
        if not r["resolved"] and per_task.get(r["id"], {}).get("must_pass")
    )
    passed = (len(rows) >= baseline["tasks"] and resolved >= floor
              and not must_pass_failed)
    return {
        "passed": passed,
        "resolved": resolved,
        "tasks_run": len(rows),
        "baseline_resolved": baseline["resolved"],
        "floor": floor,
        "must_pass_failed": must_pass_failed,
        "regressed": sorted(r["id"] for r in rows if not r["resolved"]),
    }


def fingerprint_mismatch(pinned: dict | None, live: dict) -> list[str]:
    """Compared fields whose live value differs from the pin; empty when comparable."""
    if not pinned:
        return []
    return [f"{k}: pinned {pinned.get(k)!r}, live {live.get(k)!r}"
            for k in COMPARED_FIELDS if pinned.get(k) != live.get(k)]


def pin(rows: list[dict], repeats: int, model: str, fp: dict) -> dict:
    """Write a schema 2 interim baseline from this run's rows."""
    b = {
        "schema": SCHEMA,
        "model": model,
        "tasks": len(rows),
        "resolved": sum(1 for r in rows if r["resolved"]),
        "repeats": repeats,
        "slack": 1,
        "pinned": date.today().isoformat(),
        "interim": True,
        "note": INTERIM_NOTE,
        "fingerprint": fp,
        "per_task": {
            r["id"]: {"passes": r["passes"], "runs": r["runs"],
                      "must_pass": r["passes"] == r["runs"]}
            for r in rows
        },
    }
    BASELINE_PATH.write_text(json.dumps(b, indent=2) + "\n", encoding="utf-8")
    return b


def _args(argv: list[str] | None) -> argparse.Namespace:
    """Gate CLI flags."""
    ap = argparse.ArgumentParser(description="Eval regression gate")
    ap.add_argument("--model", default=None)
    ap.add_argument("--task", action="append", default=[],
                    help="subset run (verdict FAILS on subsets by design)")
    ap.add_argument("--repeats", type=int, default=3,
                    help="match the baseline method (majority of 3)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--pin", action="store_true", help="re-pin baseline from this run")
    ap.add_argument("--allow-fingerprint-mismatch", action="store_true",
                    help="compare even when the live model digest or Ollama version differs from the pin")
    ap.add_argument("--out", default=None,
                    help="write the run's .md report here (JSON twin beside it); "
                         "--pin defaults to results/baseline-<label>-interim-<date>.md")
    return ap.parse_args(argv)


def _report_path(args: argparse.Namespace, label: str) -> Path | None:
    """Where this run's report lands; verdict runs write none unless asked."""
    if args.out:
        return Path(args.out)
    if args.pin:
        safe = label.replace(":", "_")
        return RESULTS_DIR / f"baseline-{safe}-interim-{date.today().isoformat()}.md"
    return None


def _print_verdict(v: dict, as_json: bool) -> None:
    """Human or JSON rendering of a verdict dict."""
    if as_json:
        print(json.dumps(v))
        return
    state = "PASS" if v["passed"] else "FAIL"
    print(f"gate: {state}: resolved {v['resolved']}/{v['tasks_run']} "
          f"(floor {v['floor']}, baseline {v['baseline_resolved']})")
    if v["must_pass_failed"]:
        print(f"must_pass failed: {', '.join(v['must_pass_failed'])}")
    if v["regressed"]:
        print(f"unresolved: {', '.join(v['regressed'])}")


def main(argv: list[str] | None = None) -> int:
    """CLI: fingerprint check, run, then pin or verdict."""
    args = _args(argv)
    label = args.model or "default-local"
    meta = run_meta(args.model, args.repeats, "off")
    baseline = None if args.pin else load_baseline()
    if baseline is not None:
        mismatch = fingerprint_mismatch(baseline["fingerprint"], meta["fingerprint"])
        if mismatch and not args.allow_fingerprint_mismatch:
            print("gate: fingerprint mismatch, the pinned baseline is for a different model or server:")
            print("\n".join(f"  {m}" for m in mismatch))
            print("  pass --allow-fingerprint-mismatch to compare anyway")
            return EXIT_FINGERPRINT

    rows = run_eval(args.model, args.task, args.repeats)
    out = _report_path(args, label)
    if out is not None:
        md, js = write_report(rows, meta, out)
        print(f"wrote {md} and {js}")
    if args.pin:
        b = pin(rows, args.repeats, label, meta["fingerprint"])
        print(f"pinned baseline: {json.dumps(b)}")
        return 0

    v = verdict(rows, baseline)
    _print_verdict(v, args.json)
    return 0 if v["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
