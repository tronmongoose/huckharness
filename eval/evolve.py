#!/usr/bin/env python3
"""GEPA-style prompt evolution over the HARNESS_PROMPT_OVERLAY block.

One candidate is a text block of operating notes. The loop scores a
candidate by running the private eval with the overlay set (eval/run.py,
deterministic graders, no model in the scoring), collects the failing
tasks' traces (grader detail, halt reason, error counts, and the transcript
tail when the run kept its meta dir), asks the local model to rewrite the
notes in light of those traces, and scores the child. A child joins the
front when it resolves a task no front member resolves, or resolves at
least as many as its parent while losing none. Nothing is applied: the
best block is written to eval/results/overlay-best.md and lands through
review like any other file.

Bounded by rounds, a wall-clock budget, and the overlay byte cap. Local
only: the reflection model is the harness's own Ollama default.

Exports: Candidate, failure_traces, propose, score, evolve, main.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))
from fingerprint import default_model  # noqa: E402
from run import run_eval  # noqa: E402

RESULTS_DIR = HERE / "results"
OVERLAY_CAP_BYTES = 4000
TRACE_TAIL_CHARS = 600
MAX_TRACES = 6

_SYSTEM = (
    "You improve a short block of operating notes for a local coding agent "
    "that edits files with Read, Edit, Write, Bash, Grep and Glob tools and "
    "must finish a task in a bounded number of steps. You are given the "
    "current notes and traces of tasks the agent failed. Return ONLY the "
    "revised notes as plain-text bullet lines, no heading, under 3500 bytes. "
    "Keep rules that plausibly helped, add specific rules that would have "
    "prevented the failures shown, drop rules with no evidence. Never "
    "mention task names or paths from the traces."
)


@dataclass
class Candidate:
    """One overlay text with its eval outcome."""

    text: str
    parent: int | None = None
    resolved: int = 0
    steps: float = 0.0
    passes: dict = field(default_factory=dict)
    rows: list = field(default_factory=list)
    accepted: bool = False

    def dominates_or_equals(self, other: Candidate) -> bool:
        """True when this candidate passes every task the other passes."""
        return all(self.passes.get(t, 0) >= v for t, v in other.passes.items() if v)


def _transcript_tail(meta_dir: str | None) -> str:
    """Last error-bearing tool results and the final assistant text of one run."""
    if not meta_dir:
        return ""
    logs = sorted(Path(meta_dir).glob("sessions/*.jsonl"))
    if not logs:
        return ""
    from metrics import _records
    records = _records(logs[0])
    errors = [str(r.get("content", ""))[:160] for r in records
              if r.get("kind") == "tool_result" and r.get("is_error")]
    finals = [str(r.get("content", ""))[:200] for r in records
              if r.get("kind") == "assistant_message" and r.get("content")]
    tail = "; ".join(errors[-3:]) + (" | final: " + finals[-1] if finals else "")
    return tail[:TRACE_TAIL_CHARS]


def failure_traces(rows: list[dict]) -> list[str]:
    """One compact trace per failing task, at most MAX_TRACES."""
    traces = []
    for row in rows:
        if row.get("resolved"):
            continue
        run = (row.get("per_run") or [{}])[-1]
        parts = [f"task: {row['id']}", f"grader: {row.get('detail', '')[:160]}",
                 f"halted: {run.get('halted_reason')}", f"steps: {run.get('steps')}",
                 f"edit_errors: {run.get('edit_errors', 0)}",
                 f"bash_errors_harness: {run.get('bash_errors_harness', 0)}"]
        tail = _transcript_tail(run.get("meta_dir"))
        if tail:
            parts.append(f"trace: {tail}")
        traces.append("\n".join(parts))
    return traces[:MAX_TRACES]


def _clip(text: str) -> str:
    """Overlay text under the byte cap, whole lines only."""
    data = text.strip().encode()
    if len(data) <= OVERLAY_CAP_BYTES:
        return text.strip()
    cut = data[:OVERLAY_CAP_BYTES]
    head, _, _ = cut.rpartition(b"\n")
    return (head or cut).decode("utf-8", errors="ignore").strip()


def propose(current: str, traces: list[str], model: str, chat=None) -> str:
    """Reflective mutation: the local model rewrites the notes given the traces."""
    from coding_harness.models import ollama
    ollama.assert_model_allowed(model)
    chat = chat or ollama.chat
    user = ("CURRENT NOTES:\n" + (current or "(empty)") + "\n\nFAILURE TRACES:\n\n"
            + "\n\n".join(traces))
    reply = chat(model=model, messages=[{"role": "system", "content": _SYSTEM},
                                        {"role": "user", "content": user}],
                 temperature=0.7, max_tokens=1200)
    return _clip(str(reply.get("content") or ""))


def score(text: str, model: str | None, opts: dict, run=run_eval) -> Candidate:
    """Run the eval with ``text`` as the overlay; empty text runs without one."""
    overlay = None
    if text:
        path = Path(opts["workdir"]) / f"overlay-{int(time.time() * 1000)}.md"
        path.write_text(text + "\n", encoding="utf-8")
        overlay = str(path)
    rows = run(model, opts["tasks"], opts["repeats"], keep_workdirs=opts["keep"],
               overlay=overlay)
    steps = [r["steps"] for r in rows if r.get("resolved") and r.get("steps") is not None]
    return Candidate(text=text, rows=rows,
                     resolved=sum(1 for r in rows if r.get("resolved")),
                     steps=sum(steps) / len(steps) if steps else 0.0,
                     passes={r["id"]: r.get("passes", 0) for r in rows})


def _accept(child: Candidate, parent: Candidate, front: list[Candidate]) -> bool:
    """Pareto rule: a new task resolved, or no task lost with equal or better count."""
    for task, passes in child.passes.items():
        if passes and not any(c.passes.get(task) for c in front):
            return True
    return child.resolved >= parent.resolved and child.dominates_or_equals(parent)


def _pick_parent(front: list[Candidate]) -> Candidate:
    """Most resolved, then fewest mean steps."""
    return sorted(front, key=lambda c: (-c.resolved, c.steps))[0]


def evolve(seed: str, model: str | None, opts: dict, *, rounds: int,
           budget_s: float, chat=None, run=run_eval) -> tuple[list[Candidate], int]:
    """Bounded loop; returns (all candidates, index of the best)."""
    t0 = time.monotonic()
    reflect_model = opts.get("reflect_model") or model or default_model()
    root = score(seed, model, opts, run)
    root.accepted = True
    candidates, front = [root], [root]
    for _ in range(rounds):
        if time.monotonic() - t0 > budget_s:
            print("evolve: wall budget reached, stopping")
            break
        parent = _pick_parent(front)
        traces = failure_traces(parent.rows)
        if not traces:
            print("evolve: parent resolves every task, nothing to reflect on")
            break
        child = Candidate(text=propose(parent.text, traces, reflect_model, chat),
                          parent=candidates.index(parent))
        if not child.text or child.text == parent.text:
            continue
        scored = score(child.text, model, opts, run)
        scored.parent, scored.accepted = child.parent, _accept(scored, parent, front)
        candidates.append(scored)
        if scored.accepted:
            front.append(scored)
        print(f"  round: resolved {scored.resolved} vs parent {parent.resolved}, "
              f"{'kept' if scored.accepted else 'dropped'}")
    best = candidates.index(_pick_parent(front))
    return candidates, best


def write_results(candidates: list[Candidate], best: int, out_dir: Path) -> Path:
    """Lineage JSON plus overlay-best.md; returns the JSON path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [{"index": i, "parent": c.parent, "resolved": c.resolved, "steps": c.steps,
             "passes": c.passes, "accepted": c.accepted, "text": c.text}
            for i, c in enumerate(candidates)]
    path = out_dir / f"evolve-{datetime.now().strftime('%Y-%m-%d-%H%M')}.json"
    path.write_text(json.dumps({"best": best, "candidates": rows}, indent=2) + "\n",
                    encoding="utf-8")
    (out_dir / "overlay-best.md").write_text(candidates[best].text + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Prompt evolution over the overlay block")
    ap.add_argument("--seed", default=None, help="overlay file to start from (default: empty)")
    ap.add_argument("--model", default=None, help="coder model under test")
    ap.add_argument("--reflect-model", default=None, help="Ollama model that rewrites the notes")
    ap.add_argument("--task", action="append", default=[])
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--budget-min", type=float, default=90.0)
    ap.add_argument("--keep-workdirs", action="store_true",
                    help="keep per-run meta dirs so traces include transcript tails")
    ap.add_argument("--out", default=str(RESULTS_DIR))
    args = ap.parse_args(argv)
    seed = Path(args.seed).read_text(encoding="utf-8") if args.seed else ""
    opts = {"tasks": args.task, "repeats": args.repeats, "keep": args.keep_workdirs,
            "reflect_model": args.reflect_model, "workdir": str(Path(args.out) / "evolve-work")}
    Path(opts["workdir"]).mkdir(parents=True, exist_ok=True)
    candidates, best = evolve(seed, args.model, opts, rounds=args.rounds,
                              budget_s=args.budget_min * 60)
    path = write_results(candidates, best, Path(args.out))
    print(f"evolve: {len(candidates)} candidates, best #{best} resolves "
          f"{candidates[best].resolved}, written {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
