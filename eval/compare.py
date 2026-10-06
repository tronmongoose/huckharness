#!/usr/bin/env python3
"""Reproducibility verdict between two eval report JSONs (P0-4).

Two runs are reproducible when every task's pass count differs by at most one
and the resolved totals are within the gate's slack. Prints the verdict and
exits 0 when reproducible, 1 otherwise.

Exports: load_report, reproducible, main.

Usage: python eval/compare.py A.json B.json [--slack N]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

MAX_PASS_DELTA = 1


def load_report(path: Path) -> dict:
    """The JSON twin run.py writes beside each markdown report."""
    return json.loads(path.read_text(encoding="utf-8"))


def reproducible(a_rows: list[dict], b_rows: list[dict], slack: int) -> dict:
    """Per-task pass drift plus the aggregate gap; verdict needs both inside bounds."""
    a = {r["id"]: r for r in a_rows}
    b = {r["id"]: r for r in b_rows}
    drift = {}
    for tid in sorted(set(a) | set(b)):
        pa = a.get(tid, {}).get("passes")
        pb = b.get(tid, {}).get("passes")
        if pa is None or pb is None or abs(pa - pb) > MAX_PASS_DELTA:
            drift[tid] = [pa, pb]
    resolved = [sum(1 for r in rows if r.get("resolved")) for rows in (a_rows, b_rows)]
    gap = abs(resolved[0] - resolved[1])
    return {
        "reproducible": not drift and gap <= slack,
        "drift": drift, "resolved": resolved, "aggregate_gap": gap, "slack": slack,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI: print the reproducibility verdict for two report JSONs."""
    ap = argparse.ArgumentParser(description="Eval reproducibility check")
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--slack", type=int, default=1, help="aggregate tolerance (the pinned gate slack)")
    args = ap.parse_args(argv)

    ra, rb = load_report(Path(args.a)), load_report(Path(args.b))
    r = reproducible(ra.get("rows") or [], rb.get("rows") or [], args.slack)
    state = "REPRODUCIBLE" if r["reproducible"] else "NOT REPRODUCIBLE"
    print(f"compare: {state}: resolved {r['resolved'][0]} vs {r['resolved'][1]} "
          f"(gap {r['aggregate_gap']}, slack {r['slack']})")
    for tid, (pa, pb) in r["drift"].items():
        print(f"  drift {tid}: passes {pa} vs {pb}")
    for label, rep in (("a", ra), ("b", rb)):
        fp = rep.get("fingerprint") or {}
        print(f"  {label}: sha {fp.get('harness_sha')} model {fp.get('model')} "
              f"digest {fp.get('model_digest')}")
    return 0 if r["reproducible"] else 1


if __name__ == "__main__":
    sys.exit(main())
