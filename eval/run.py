#!/usr/bin/env python3
"""Private harness eval (P1-2, runner v2 in P0-4): replay frozen tasks under a
pinned child environment, score resolved-rate, steps and per-session metrics.

Each task under eval/tasks/<id>/ is a `task.json` (id, prompt, grade spec) plus a
`repo/` snapshot of the pre-fix files. The runner copies the snapshot into a
fresh temp dir with NO git history, so an agent cannot recover the fix via
`git log` (the Commit0 failure, SOTA report S9), drives the harness against it
in one-shot print mode with every harness switch pinned by `_child_env`, then
applies the deterministic grader and reads the session transcript and audit
chain out of a per-run HARNESS_META_DIR. Eval runs never touch the fleet's
chain, and the operator's shell never leaks into a result.

Headline metrics (pass-bars declared up front, per the sl-2uot pattern):
  - resolved-rate    : fraction of tasks the grader passes
  - median steps     : median harness turns over resolved tasks
Baselines are pinned per model with a fingerprint (eval/fingerprint.py) and
gated per task (eval/gate.py). Two runs whose JSON reports pass
eval/compare.py are reproducible; that is the precondition for any routing or
self-improvement change (P1-3, P2-1) to claim it moved these numbers.

Exports: run_eval, run_meta, write_report, main (the tests also use
_child_env, _run_harness, _report).

Usage: python eval/run.py [--model NAME] [--task ID ...] [--repeats N]
                          [--review off|local|claude-cli] [--review-model TAG]
                          [--keep-workdirs] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import date
from pathlib import Path

from fingerprint import default_model, fingerprint
from grader import grade
from metrics import session_metrics

HERE = Path(__file__).resolve().parent
TASKS_DIR = HERE / "tasks"
RESULTS_DIR = HERE / "results"
HARNESS_MAX_TIME_S = 540
HARNESS_TIMEOUT_S = 600
PINNED_SWITCHES = {
    "HARNESS_VERIFY_REPAIR": "1",
    "HARNESS_REPO_MAP": "1",
    "HARNESS_EDIT_HINT": "1",
    "HARNESS_TOOL_PROBE": "1",
    "HARNESS_DONE_GATE": "1",
    "HARNESS_TARGETED_TESTS": "1",
}


def _load_tasks(filter_ids: list[str]) -> list[dict]:
    """Task specs in id order, optionally restricted to filter_ids."""
    tasks = []
    for tj in sorted(TASKS_DIR.glob("*/task.json")):
        spec = json.loads(tj.read_text(encoding="utf-8"))
        spec["_dir"] = tj.parent
        spec["grade"]["task_dir"] = str(tj.parent.resolve())
        if not filter_ids or spec["id"] in filter_ids:
            tasks.append(spec)
    return tasks


def _prepare(task: dict) -> Path:
    """Copy the task's pre-fix snapshot into a fresh, history-stripped dir."""
    wd = Path(tempfile.mkdtemp(prefix=f"eval-{task['id']}-"))
    src = task["_dir"] / "repo"
    for p in src.rglob("*"):
        if p.is_file():
            dst = wd / p.relative_to(src)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
    return wd  # deliberately NO .git, nothing for the agent to mine


HERMETIC_SWITCHES = {
    "HARNESS_SETTINGS": "off",
    "HARNESS_RECALL": "0",
    "HARNESS_MEMORY_PROPOSALS": "0",
}
# The only operator variables the child inherits. PATH, PYTHONPATH and
# VIRTUAL_ENV locate the interpreter, ruff, pytest and this checkout; the
# Ollama pair locates the server; USER and LOGNAME name the account the
# claude CLI's keychain login is filed under. Everything else, including any
# HARNESS_* switch in the operator's shell, is left behind.
INHERITED_VARS = ("PATH", "PYTHONPATH", "VIRTUAL_ENV", "OLLAMA_URL", "OLLAMA_HOST",
                  "LANG", "LC_ALL", "TMPDIR", "TERM", "USER", "LOGNAME")


def _review_vars(review: str, review_model: str | None) -> dict:
    """The review switches for one row, with the real home for a claude-cli reviewer."""
    env = {"HARNESS_REVIEW": "0" if review == "off" else "1"}
    if review != "off":
        env["HARNESS_REVIEW_BACKEND"] = review
    if review == "claude-cli":
        env["HARNESS_CLAUDE_HOME"] = os.path.expanduser("~")
    if review_model:
        env["HARNESS_REVIEW_MODEL"] = review_model
    return env


def _child_env(review: str, review_model: str | None, meta_dir: str,
               overlay: str | None = None, home_dir: str | None = None) -> dict:
    """Only allowlisted operator variables, every harness switch pinned.

    ANTHROPIC_API_KEY is never inherited: with it set, a claude-cli review
    row bills the (empty) API account instead of the Max plan. The prompt
    overlay reaches the child only through ``overlay``, so a stale
    HARNESS_PROMPT_OVERLAY in the shell cannot score a different prompt.

    Every row runs under HOME = ``home_dir`` (default ``<meta_dir>/home``)
    with memory under the meta dir. The pins alone do not isolate the child:
    skills and the global AGENTS.md are read from HOME, and one run wrote a
    memory into the real corpus. A claude-cli reviewer needs its login, so
    the real home rides HARNESS_CLAUDE_HOME and only the CLI subprocess
    (models/claude_cli.py) gets it back as HOME.
    """
    env = {k: os.environ[k] for k in INHERITED_VARS if k in os.environ}
    env.update(PINNED_SWITCHES)
    env.update(HERMETIC_SWITCHES)
    env.update(_review_vars(review, review_model))
    env["HARNESS_META_DIR"] = meta_dir
    env["HARNESS_MEMORY_DIR"] = os.path.join(meta_dir, "memory")
    env["HOME"] = home_dir or os.path.join(meta_dir, "home")
    if overlay:
        env["HARNESS_PROMPT_OVERLAY"] = overlay
    # The child's cwd is a task tempdir, where an editable install would win
    # over this checkout; PYTHONPATH keeps the fingerprinted sha and the code
    # under test the same thing.
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(HERE.parent), env.get("PYTHONPATH")) if p
    )
    return env


def _summary(stderr: str) -> dict:
    """Fields from the harness's final stderr summary line, empty when absent."""
    out: dict = {}
    for line in stderr.splitlines():
        line = line.strip()
        if not (line.startswith("{") and "summary" in line):
            continue
        try:
            s = json.loads(line)
        except json.JSONDecodeError:
            continue
        if s.get("event") == "summary":
            out = {"steps": s.get("turns"), "halted_reason": s.get("halted_reason"),
                   "files_changed": s.get("files_changed") or []}
    return out


def _run_harness(workdir: Path, prompt: str, model: str | None, env: dict,
                 checks: list[str] | None = None) -> dict:
    """One print-mode session: steps, halted_reason, files_changed, wall_s."""
    cmd = [sys.executable, "-m", "coding_harness", "--force-local", "--no-mcp",
           "--max-time", str(HARNESS_MAX_TIME_S)]
    if model:
        cmd += ["--model", model]
    for check in checks or []:
        cmd += ["--check", check]
    cmd.append(prompt)
    t0 = time.monotonic()
    halted = None
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), capture_output=True, text=True,
            timeout=HARNESS_TIMEOUT_S, env=env,
        )
        stderr = proc.stderr
    except subprocess.TimeoutExpired:
        stderr, halted = "", "timeout"
    out = {"steps": None, "halted_reason": halted, "files_changed": [],
           "wall_s": round(time.monotonic() - t0, 1)}
    out.update(_summary(stderr))
    out["files_changed"] = [_relative(f, workdir) for f in out["files_changed"]]
    return out


def _relative(path: str, workdir: Path) -> str:
    """``path`` relative to the task workdir, so results carry no machine paths."""
    try:
        return str(Path(path).resolve().relative_to(workdir.resolve()))
    except ValueError:
        return Path(path).name


def _session_log(meta_dir: Path) -> Path:
    """The run's sole transcript, or a nonexistent path when the harness wrote none."""
    logs = sorted((meta_dir / "sessions").glob("*.jsonl"))
    return logs[0] if logs else meta_dir / "sessions" / "none.jsonl"


def _run_once(task: dict, model: str | None, opts: dict) -> dict:
    """Prepare, run, grade and measure one repeat of a task."""
    wd = _prepare(task)
    meta = Path(tempfile.mkdtemp(prefix=f"eval-meta-{task['id']}-"))
    home = Path(tempfile.mkdtemp(prefix=f"eval-home-{task['id']}-"))
    env = _child_env(opts["review"], opts["review_model"], str(meta), opts["overlay"],
                     home_dir=str(home))
    run = _run_harness(wd, task["prompt"], model, env, task.get("checks"))
    passed, detail = grade(task["grade"], wd)
    metrics = session_metrics(_session_log(meta), meta)
    # The parent too: the grader truncates detail, sometimes mid-path.
    for prefix in (str(wd.resolve()), str(wd), str(wd.resolve().parent), str(wd.parent)):
        detail = detail.replace(prefix, ".")
    row = {**metrics, "passed": passed, "detail": detail, **run}
    # A harness that dies before its summary line leaves only the transcript to say why.
    row["halted_reason"] = run["halted_reason"] or metrics["halted_reason"]
    if opts["keep"]:
        row["meta_dir"] = str(meta)
        print(f"    kept workdir {wd} meta {meta} home {home}")
    else:
        shutil.rmtree(wd, ignore_errors=True)
        shutil.rmtree(meta, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
    return row


def _task_row(task_id: str, runs: list[dict]) -> dict:
    """Majority-resolved task row with per-run rows and per-task aggregates."""
    passes = sum(1 for r in runs if r["passed"])
    pass_steps = [r["steps"] for r in runs if r["passed"] and r["steps"] is not None]
    resolved = passes * 2 >= len(runs)
    return {
        "id": task_id, "resolved": resolved, "passes": passes, "runs": len(runs),
        "steps": statistics.median(pass_steps) if pass_steps else None,
        "detail": "" if resolved else runs[-1]["detail"],
        "wall_s": statistics.median([r["wall_s"] for r in runs]),
        "edit_errors": sum(r["edit_errors"] for r in runs),
        "bash_errors_harness": sum(r["bash_errors_harness"] for r in runs),
        "repair_rounds": sum(r["repair_rounds"] for r in runs),
        "per_run": runs,
    }


def run_eval(model: str | None, filter_ids: list[str], repeats: int = 1, *,
             review: str = "off", review_model: str | None = None,
             keep_workdirs: bool = False, overlay: str | None = None) -> list[dict]:
    """Run each task ``repeats`` times. The local model is noisy run-to-run
    (SOTA report S34), so a task's score is its pass-fraction over the repeats,
    and it counts as resolved on a majority. steps is the median over passing
    runs."""
    opts = {"review": review, "review_model": review_model, "keep": keep_workdirs,
            "overlay": overlay}
    rows = []
    for task in _load_tasks(filter_ids):
        runs = [_run_once(task, model, opts) for _ in range(repeats)]
        row = _task_row(task["id"], runs)
        rows.append(row)
        print(f"  {'RESOLVED' if row['resolved'] else 'FAILED  '} {row['id']:<24} "
              f"{row['passes']}/{repeats} pass, steps={row['steps']}, wall={row['wall_s']}s")
    return rows


def run_meta(model: str | None, repeats: int, review: str) -> dict:
    """Report header: model label and tag, repeats, review setting, date, fingerprint."""
    tag = model or default_model()
    return {"model": model or "default-local", "model_tag": tag, "repeats": repeats,
            "review": review, "date": date.today().isoformat(), "fingerprint": fingerprint(tag)}


def _report(rows: list[dict], model: str, repeats: int, fp: dict) -> str:
    """Markdown report: headline numbers, per-task table, fingerprint footer."""
    n = len(rows)
    resolved = [r for r in rows if r["resolved"]]
    rate = len(resolved) / n if n else 0.0
    steps = [r["steps"] for r in resolved if r["steps"] is not None]
    med = statistics.median(steps) if steps else None
    lines = [
        f"# Harness eval baseline: model `{model}`",
        "",
        f"- tasks: {n}, repeats/task: {repeats}",
        f"- resolved-rate (majority): {rate:.0%} ({len(resolved)}/{n})",
        f"- median steps (resolved): {med}",
        "",
        "| task | pass/runs | pass frac | steps | wall s | edit err | bash harness err | repairs | note |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        note = " ".join(r["detail"].split())[:60]
        lines.append(
            f"| {r['id']} | {r['passes']}/{r['runs']} | {r['passes'] / r['runs']:.2f} "
            f"| {r['steps']} | {r['wall_s']} | {r['edit_errors']} | {r['bash_errors_harness']} "
            f"| {r['repair_rounds']} | {note} |"
        )
    lines += ["", "Fingerprint:", ""] + [f"- {k}: {v}" for k, v in fp.items()]
    return "\n".join(lines) + "\n"


def write_report(rows: list[dict], meta: dict, md_path: Path) -> tuple[Path, Path]:
    """Write the markdown report and its JSON twin (meta + rows) beside it."""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(_report(rows, meta["model"], meta["repeats"], meta["fingerprint"]),
                       encoding="utf-8")
    js_path = md_path.with_suffix(".json")
    js_path.write_text(json.dumps({**meta, "rows": rows}, indent=2) + "\n", encoding="utf-8")
    return md_path, js_path


def main() -> int:
    """CLI: run the suite, print and write the report."""
    ap = argparse.ArgumentParser(description="Private harness eval")
    ap.add_argument("--model", default=None, help="Ollama model (default: harness default)")
    ap.add_argument("--task", action="append", default=[], help="run only this task id (repeatable)")
    ap.add_argument("--repeats", type=int, default=1, help="runs per task (>=3 for a stable baseline)")
    ap.add_argument("--review", choices=["off", "local", "claude-cli"], default="off",
                    help="agentic review backend in the child sessions (default off)")
    ap.add_argument("--review-model", default=None, help="HARNESS_REVIEW_MODEL for the child sessions")
    ap.add_argument("--keep-workdirs", action="store_true",
                    help="leave task workdirs and meta dirs on disk and print their paths")
    ap.add_argument("--out", default=None, help="write the .md report here (JSON twin beside it)")
    args = ap.parse_args()

    label = args.model or "default-local"
    print(f"Running harness eval (model={label}, repeats={args.repeats}, review={args.review})...")
    meta = run_meta(args.model, args.repeats, args.review)
    rows = run_eval(args.model, args.task, args.repeats, review=args.review,
                    review_model=args.review_model, keep_workdirs=args.keep_workdirs)
    out = Path(args.out) if args.out else RESULTS_DIR / f"baseline-{label.replace(':', '_')}.md"
    md, js = write_report(rows, meta, out)
    print(f"\n{md.read_text(encoding='utf-8')}\nwrote {md} and {js}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
