"""Bucket Claude Code sessions and fallback launches so the migration backlog
comes from data. Exports: scan_sessions, summarize, fallbacks, main."""
from __future__ import annotations

import argparse
import collections
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from coding_harness.core.paths import meta_dir

PROJECTS = Path.home() / ".claude" / "projects"
FALLBACK_LOG = meta_dir() / "claude_fallback.jsonl"
RESULTS = Path(__file__).resolve().parent / "results"
SKIP_DIRS = ("nsreview", "-private-tmp")
MAX_FILES = 5000
_FEATURES = {
    "plan": ("EnterPlanMode", "ExitPlanMode"),
    "worktree": ("EnterWorktree",),
    "subagent": ("Agent", "Workflow"),
    "memory": ("memory/",),
    "mcp": ("mcp__",),
    "artifact": ("Artifact",),
    "web": ("WebFetch", "WebSearch"),
}


def _repo(dirname: str) -> str:
    """Repo name from a Claude Code project dir name (hyphens in names kept)."""
    for marker in ("-projects-", "-orca-workspaces-"):
        if marker in dirname:
            return dirname.split(marker, 1)[1]
    return dirname.lstrip("-") or dirname


def _session(path: Path) -> dict[str, Any] | None:  # LONG-FN: one pass over one log
    """One session's counters, or None when the log has no user prompt."""
    tools: collections.Counter[str] = collections.Counter()
    first_prompt = None
    turns = 0
    writes: list[str] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for line in lines:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        kind = r.get("type")
        msg = r.get("message") or {}
        content = msg.get("content")
        if kind == "user" and isinstance(content, str) and first_prompt is None:
            first_prompt = content[:120].replace("\n", " ")
        if kind == "assistant":
            turns += 1
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_use":
                tools[str(c.get("name"))] += 1
                target = str((c.get("input") or {}).get("file_path", ""))
                if c.get("name") in ("Write", "Edit"):
                    writes.append(target)
    if first_prompt is None:
        return None
    names = set(tools) | {w for w in writes}
    features = sorted(
        f for f, keys in _FEATURES.items() if any(k in n for k in keys for n in names)
    )
    return {
        "session": path.stem,
        "repo": _repo(path.parent.name),
        "turns": turns,
        "tool_calls": sum(tools.values()),
        "tools": dict(tools.most_common(6)),
        "features": features,
        "first_prompt": first_prompt,
        "mtime": int(path.stat().st_mtime),
    }


def scan_sessions(days: int) -> list[dict[str, Any]]:
    """Every non-reviewer session log touched in the last ``days``."""
    cutoff = time.time() - days * 86400
    out = []
    for d in sorted(PROJECTS.iterdir()) if PROJECTS.is_dir() else []:
        if any(s in d.name for s in SKIP_DIRS):
            continue
        for path in sorted(d.glob("*.jsonl"))[:MAX_FILES]:
            if path.stat().st_mtime < cutoff:
                continue
            rec = _session(path)
            if rec:
                out.append(rec)
    return out


def summarize(sessions: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate by repo and by feature; these decide the stage-3 order."""
    by_repo: collections.Counter[str] = collections.Counter()
    by_feature: collections.Counter[str] = collections.Counter()
    by_tool: collections.Counter[str] = collections.Counter()
    for s in sessions:
        by_repo[s["repo"]] += 1
        for f in s["features"]:
            by_feature[f] += 1
        for t, n in s["tools"].items():
            by_tool[t] += n
    turns = sorted(s["turns"] for s in sessions) or [0]
    return {
        "sessions": len(sessions),
        "median_turns": turns[len(turns) // 2],
        "by_repo": dict(by_repo.most_common()),
        "by_feature": dict(by_feature.most_common()),
        "by_tool": dict(by_tool.most_common(12)),
    }


def fallbacks(days: int) -> list[dict[str, Any]]:
    """Lines the ``claude`` shell wrapper appended in the last ``days``."""
    if not FALLBACK_LOG.is_file():
        return []
    cutoff = time.time() - days * 86400
    rows = []
    for line in FALLBACK_LOG.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if float(r.get("ts", 0)) >= cutoff:
            rows.append(r)
    return rows


def _table(sessions: list[dict[str, Any]], rows: int) -> str:
    """Top sessions by tool calls, one line each."""
    top = sorted(sessions, key=lambda s: -s["tool_calls"])[:rows]
    lines = [f"{'repo':14} {'turns':>5} {'calls':>5} features         prompt"]
    for s in top:
        feats = ",".join(s["features"])[:16]
        lines.append(f"{s['repo'][:14]:14} {s['turns']:5d} {s['tool_calls']:5d} "
                     f"{feats:16} {s['first_prompt'][:60]}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI: session buckets by default, ``--fallbacks`` for the wrapper log."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--rows", type=int, default=20)
    ap.add_argument("--fallbacks", action="store_true")
    ap.add_argument("--out", default=None, help="json path (default eval/results)")
    args = ap.parse_args(argv)
    if args.fallbacks:
        for r in fallbacks(args.days):
            ts = time.strftime("%m-%d %H:%M", time.localtime(float(r.get("ts", 0))))
            print(f"{ts}  {os.path.basename(r.get('cwd', '')):20} {r.get('reason', '')}")
        return 0
    sessions = scan_sessions(args.days)
    summary = summarize(sessions)
    out = Path(args.out) if args.out else RESULTS / f"claude-usage-{time.strftime('%Y-%m-%d')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "sessions": sessions}, indent=1))
    print(json.dumps(summary, indent=1))
    print()
    print(_table(sessions, args.rows))
    print(f"\nwrote {out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
