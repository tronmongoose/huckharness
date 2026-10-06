"""Deterministic graders for the private harness eval (P1-2).

A grader takes a spec dict and the task working directory, and returns
(passed: bool, detail: str). Graders are DETERMINISTIC — no model in the loop —
so a task's outcome is reproducible. Add graders here as new task shapes appear.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path


def _ruff_clean(spec: dict, workdir: Path) -> tuple[bool, str]:
    target = workdir / spec["target"]
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--select", "F,I,UP,B,E,W",
         "--ignore", "E501", str(target)],
        capture_output=True, text=True, timeout=30,
    )
    ok = proc.returncode == 0
    return ok, "ruff clean" if ok else (proc.stdout or proc.stderr).strip()[:200]


def _file_contains(spec: dict, workdir: Path) -> tuple[bool, str]:
    text = (workdir / spec["target"]).read_text(encoding="utf-8")
    needle = spec["text"]
    ok = needle in text
    return ok, f"{'found' if ok else 'missing'}: {needle!r}"


def _file_not_contains(spec: dict, workdir: Path) -> tuple[bool, str]:
    ok, detail = _file_contains(spec, workdir)
    return (not ok), detail


def _command(spec: dict, workdir: Path) -> tuple[bool, str]:
    """Run a shell command in the workdir; pass on the expected exit code."""
    proc = subprocess.run(
        spec["cmd"], shell=True, cwd=str(workdir),
        capture_output=True, text=True, timeout=60,
    )
    want = spec.get("expect_exit", 0)
    ok = proc.returncode == want
    return ok, f"exit {proc.returncode} (want {want}): {(proc.stderr or proc.stdout).strip()[:160]}"


def _hidden_tests(spec: dict, workdir: Path) -> tuple[bool, str]:
    """Overwrite the listed files from the task's hidden/ dir, then run cmd as _command does.

    The agent never sees these files: they are the merged PR's tests, so an
    agent that rewrote or deleted the visible test cannot grade itself.
    """
    hidden = Path(spec["task_dir"]) / "hidden"
    for rel in spec["files"]:
        dst = workdir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(hidden / rel, dst)
    return _command(spec, workdir)


_GRADERS = {
    "ruff_clean": _ruff_clean,
    "file_contains": _file_contains,
    "file_not_contains": _file_not_contains,
    "command": _command,
    "hidden_tests": _hidden_tests,
}


def grade(spec: dict, workdir: Path) -> tuple[bool, str]:
    """Dispatch to the grader named by spec['type']. Unknown type fails."""
    fn = _GRADERS.get(spec.get("type", ""))
    if fn is None:
        return False, f"unknown grader type {spec.get('type')!r}"
    try:
        return fn(spec, workdir)
    except Exception as e:  # noqa: BLE001 — a broken grader must fail, not crash the suite
        return False, f"grader error: {type(e).__name__}: {e}"
