#!/usr/bin/env python3
"""Model step-time analysis (P0-0) from existing harness session logs.

A model step is the gap between an assistant_message (or a timeout error in
its place) and the record before it. Reports p50/p90/p99/max, counts of steps
over 120s and 180s, and for every session that timed out, how many other
sessions had a step in flight at that instant. No sessions are re-executed.

Exports: parse_ts, load_sessions, step_intervals, percentiles, overlap_count,
analyze, main.

Usage: python eval/step_times.py [--since YYYY-MM-DD] [--dir PATH] [--out PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from coding_harness.core.paths import meta_dir

HERE = Path(__file__).resolve().parent
RESULTS_DIR = HERE / "results"
SESSIONS_DIR = meta_dir() / "sessions"
SLOW_S = 120.0
TIMEOUT_S = 180.0


def parse_ts(ts: str) -> datetime:
    """ISO-8601 with trailing Z to an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _read_jsonl(path: Path) -> list[dict]:
    """Records of one session file; malformed lines are skipped."""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def load_sessions(sessions_dir: Path, since: date) -> dict[str, list[dict]]:
    """Session name -> records, for sessions whose first record is on or after since."""
    floor = datetime(since.year, since.month, since.day, tzinfo=timezone.utc)
    out: dict[str, list[dict]] = {}
    for path in sorted(sessions_dir.glob("*.jsonl")):
        records = [r for r in _read_jsonl(path) if "ts" in r and "kind" in r]
        if records and parse_ts(records[0]["ts"]) >= floor:
            out[path.stem] = records
    return out


def step_intervals(records: list[dict]) -> list[dict]:
    """(start, end, seconds, timed_out) per model step in one session."""
    steps = []
    for prev, rec in zip(records, records[1:]):
        timed_out = rec["kind"] == "error" and "timed out" in str(rec.get("error", ""))
        if rec["kind"] != "assistant_message" and not timed_out:
            continue
        start, end = parse_ts(prev["ts"]), parse_ts(rec["ts"])
        steps.append({"start": start, "end": end, "turn": rec.get("turn"),
                      "seconds": (end - start).total_seconds(), "timed_out": timed_out})
    return steps


def percentiles(values: list[float]) -> dict:
    """Nearest-rank p50/p90/p99 plus max over a list of seconds."""
    if not values:
        return {"n": 0, "p50": None, "p90": None, "p99": None, "max": None}
    s = sorted(values)
    n = len(s)

    def rank(p: float) -> float:
        return s[max(0, math.ceil(p * n) - 1)]

    return {"n": n, "p50": rank(0.50), "p90": rank(0.90), "p99": rank(0.99), "max": s[-1]}


def overlap_count(instant: datetime, others: dict[str, list[dict]]) -> list[str]:
    """Names of other sessions with a step spanning the instant."""
    hits = []
    for name, steps in others.items():
        if any(st["start"] <= instant <= st["end"] for st in steps):
            hits.append(name)
    return hits


def analyze(sessions: dict[str, list[dict]]) -> dict:
    """Distribution, slow-step counts, and per-timeout overlap over all sessions."""
    steps_by = {name: step_intervals(recs) for name, recs in sessions.items()}
    completed = [st["seconds"] for steps in steps_by.values()
                 for st in steps if not st["timed_out"]]
    timeouts = []
    for name, steps in steps_by.items():
        for st in steps:
            if not st["timed_out"]:
                continue
            others = {k: v for k, v in steps_by.items() if k != name}
            hits = overlap_count(st["end"], others)
            timeouts.append({"session": name, "turn": st["turn"],
                             "ts": st["end"].isoformat(), "step_seconds": st["seconds"],
                             "overlap": len(hits), "overlapping_sessions": hits})
    return {
        "sessions": len(sessions),
        "steps_completed": len(completed),
        "percentiles_s": percentiles(completed),
        "over_120s": sum(1 for s in completed if s > SLOW_S),
        "over_180s": sum(1 for s in completed if s > TIMEOUT_S),
        "timeouts": timeouts,
        "timeouts_with_overlap": sum(1 for t in timeouts if t["overlap"] > 0),
    }


def _summary(report: dict) -> str:
    """Human-readable stdout block."""
    p = report["percentiles_s"]

    def fmt(v: float | None) -> str:
        return "n/a" if v is None else f"{v:.1f}"

    lines = [
        f"sessions since {report['since']}: {report['sessions']}",
        f"completed model steps: {report['steps_completed']}",
        f"step seconds p50 {fmt(p['p50'])}  p90 {fmt(p['p90'])}  "
        f"p99 {fmt(p['p99'])}  max {fmt(p['max'])}",
        f"steps > 120s: {report['over_120s']}   steps > 180s: {report['over_180s']}",
        f"timed-out sessions: {len(report['timeouts'])} "
        f"(with another step in flight: {report['timeouts_with_overlap']})",
    ]
    for t in report["timeouts"]:
        lines.append(f"  {t['session']} turn {t['turn']} at {t['ts']} "
                     f"after {t['step_seconds']:.0f}s, overlap {t['overlap']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Model step-time analysis")
    ap.add_argument("--since", default="2026-08-20")
    ap.add_argument("--dir", default=str(SESSIONS_DIR))
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    since = date.fromisoformat(args.since)
    report = analyze(load_sessions(Path(args.dir), since))
    report["since"] = since.isoformat()
    print(_summary(report))

    RESULTS_DIR.mkdir(exist_ok=True)
    out = Path(args.out) if args.out else RESULTS_DIR / f"step-times-{date.today().isoformat()}.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
