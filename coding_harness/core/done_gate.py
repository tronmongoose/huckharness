"""Green-before-done gate: the project's checks run before the session says done (P1-4).

Exports: CheckReport, Gate, MAX_GATE_ROUNDS, PYTEST, pytest_cmd,
discover_test_cmd, targeted_tests, run_checks, baseline, compare, resolve_cmds,
run_baseline, run_gate, targeted_report. Ported from a deployment's post-PR
run_verification (same prefix allowlist, same failure extraction) so the checks
run inside the loop, where the model can still repair. A red baseline is
compared, not trusted: the gate fails only on failures the prompt introduced
and says so when it cannot tell, which is why pytest runs without -x: a
first-failure stop would leave both failure sets incomplete. Gate and the
run_* helpers keep session.py short.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from coding_harness.context import toolprobe

PYTEST = [sys.executable, "-m", "pytest", "-q"]  # explicit interpreter: PATH need not carry the venv
MAX_GATE_ROUNDS, MAX_TARGETED_FILES = 2, 5
BASELINE_CAP_S, GATE_CAP_S, TARGETED_CAP_S = 120.0, 300.0, 120.0
_FAILED_RE = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.M)
_SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def _text(path: Path) -> str:
    """File text for layout sniffing and grep; never raises on encoding."""
    return path.read_text(encoding="utf-8", errors="replace")


@dataclass
class CheckReport:
    """Outcome of one run_checks call; passed=None means the gate could not decide."""
    passed: bool | None
    failures: list[str] = field(default_factory=list)
    report: str = ""
    skipped_reason: str | None = None


@dataclass
class Gate:
    """Per-prompt state: the commands, the pre-edit failure set, rounds spent, last verdict."""
    cmds: list[str]
    before: set[str] = field(default_factory=set)
    rounds: int = 0
    passed: bool | None = None
    report: str = ""
    edits_at_run: int = 0  # the session's edit count when the gate last ran
    # Taken lazily, before the first call that can change files, so a prompt
    # that only reads never pays for a full test run.
    baselined: bool = False


def pytest_cmd(cwd: str) -> list[str]:
    """PYTEST on the target repo's interpreter (its venv via the tool probe), else our own."""
    python = toolprobe.probe(cwd).get("tools", {}).get("python", {}).get("path")
    return [python or sys.executable, *PYTEST[1:]]


def discover_test_cmd(cwd: str) -> list[str] | None:
    """The project's test command from its layout, or None when there is none to find."""
    root = Path(cwd)
    pyproject, makefile, package = root / "pyproject.toml", root / "Makefile", root / "package.json"
    # A declared make target is the project's own gate and knows what to skip
    # (fixtures meant to fail, slow suites); bare pytest collects everything.
    if makefile.is_file() and re.search(r"^test:", _text(makefile), re.M):
        return ["make", "test"]
    if (root / "tests").is_dir() or (pyproject.is_file() and "pytest" in _text(pyproject)):
        return pytest_cmd(cwd)
    if package.is_file():
        try:
            if "test" in (json.loads(_text(package)).get("scripts") or {}):
                return ["npm", "test"]
        except (ValueError, AttributeError):
            pass
    if (root / "go.mod").is_file():
        return ["go", "test", "./..."]
    if (root / "Cargo.toml").is_file():
        return ["cargo", "test"]
    return None


def _test_files(cwd: Path) -> list[Path]:
    """Every test_*.py under cwd, skipping vendored and hidden trees."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(cwd):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        found += [Path(dirpath) / f for f in sorted(filenames)
                  if f.startswith("test_") and f.endswith(".py")]
    return found


def _stem_hits(stem: str, candidates: list[Path]) -> list[Path]:
    """Tests naming the stem, the ones importing it as a module first."""
    escaped = re.escape(stem)
    word = re.compile(rf"\b{escaped}\b")
    importer = re.compile(
        rf"^\s*(?:from\s+[\w.]*\b{escaped}\b[\w.]*\s+import\b|import\s+[\w., ]*\b{escaped}\b)", re.M,
    )
    texts = [(c, _text(c)) for c in candidates]
    naming = [(c, t) for c, t in texts if word.search(t)]
    imports = [c for c, t in naming if importer.search(t)]
    return imports + [c for c, t in naming if c not in imports]


def targeted_tests(edited_paths: list[str], cwd: str) -> tuple[list[str], str | None]:
    """Tests for the edited .py files plus a note when the list was cut at MAX_TARGETED_FILES.

    An edited test is its own target; otherwise test_<stem>.py, else the tests
    naming the stem with the ones importing it first, so the cut keeps them.
    """
    paths = [Path(p) for p in edited_paths if p.endswith(".py")]
    picked = [str(p) for p in paths if p.name.startswith("test_")]
    stems = [p.stem for p in paths if not p.name.startswith("test_")]
    candidates = _test_files(Path(cwd)) if stems else []
    for stem in stems:
        hits = [c for c in candidates if c.name == f"test_{stem}.py"] or _stem_hits(stem, candidates)
        picked += [str(c) for c in hits]
    files = list(dict.fromkeys(picked))
    note = None
    if len(files) > MAX_TARGETED_FILES:
        note = (f"{len(files)} test files match this edit; ran the first "
                f"{MAX_TARGETED_FILES} (importers first)")
    return files[:MAX_TARGETED_FILES], note


def _allowed(cmd: str) -> bool:
    """The ported prefix allowlist: test runners, build tools, ruff, python -m pytest, bash <script>."""
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return False
    head = Path(argv[0]).name if argv else ""
    if head.startswith("python"):
        return argv[1:3] == ["-m", "pytest"]
    if head == "bash":
        return len(argv) > 1 and not argv[1].startswith("-")
    return head in ("pytest", "make", "npm", "go", "cargo", "ruff")


def run_checks(cmds: list[str], cwd: str, timeout_s: float) -> CheckReport:
    """Run allowlisted commands in order under one shared wall-clock budget."""
    if not cmds:
        return CheckReport(None, skipped_reason="no checks")
    deadline = time.monotonic() + timeout_s
    lines: list[str] = []
    failures: list[str] = []
    ran = 0
    for cmd in cmds:
        if not _allowed(cmd):
            lines.append(f"SKIP (not in allowlist): {cmd}")
            continue
        left = deadline - time.monotonic()
        try:
            if left <= 0:
                raise subprocess.TimeoutExpired(cmd, 0)
            proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, cwd=cwd,
                                  timeout=left)
        except subprocess.TimeoutExpired:
            lines.append(f"TIMEOUT: {cmd}")
            failures.append(f"TIMEOUT: {cmd}")
            continue
        ran += 1
        if proc.returncode == 0:
            lines.append(f"PASS: {cmd}")
            continue
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        lines += [f"FAIL: {cmd}", *(f"       {ln}" for ln in out.splitlines()[-40:])]
        failures += _FAILED_RE.findall(out) or [cmd]
    report = "\n".join(lines)[:3000]
    if ran == 0 and not failures:
        return CheckReport(None, [], report, "no runnable checks")
    return CheckReport(not failures, failures, report)


def baseline(cmds: list[str], cwd: str, timeout_s: float) -> set[str]:
    """Failure ids before the model's first step, so the gate blames only new ones."""
    return set(run_checks(cmds, cwd, timeout_s).failures)


def compare(before: set[str], after: CheckReport) -> CheckReport:
    """Pass when nothing new failed; undecidable when only baseline failures remain."""
    if after.passed is None or not after.failures:
        return after
    new = [f for f in after.failures if f not in before]
    if new:
        return CheckReport(False, new, after.report)
    return CheckReport(None, after.failures, after.report, f"baseline_red:{len(before)} failures")


def resolve_cmds(done_checks: list[str], test_cmd: list[str] | None, cwd: str) -> list[str]:
    """Explicit checks win; else the pinned test command; else discovery."""
    cmd = None if done_checks else (test_cmd or discover_test_cmd(cwd))
    return list(done_checks) or ([shlex.join(cmd)] if cmd else [])


def _budget(cap: float, share: float, remaining_s: float | None) -> float:
    """The cap, or the given share of the turn's remaining deadline when smaller."""
    return cap if remaining_s is None else min(cap, share * remaining_s)


def run_baseline(gate: Gate, cwd: str, remaining_s: float | None) -> int:
    """Record the failing set before the first mutating call; returns its size."""
    gate.before = baseline(gate.cmds, cwd, _budget(BASELINE_CAP_S, 0.2, remaining_s))
    gate.baselined = True
    return len(gate.before)


def may_mutate(tool_name: str, args: dict, cwd: str) -> bool:
    """Whether a call can change the files the checks read.

    Bash counts as read-only when the autonomy ladder would allow it at
    ``off``, the level that admits only read commands.
    """
    if tool_name in ("Edit", "Write"):
        return True
    if tool_name != "Bash":
        return False
    from coding_harness.core.mode import Autonomy
    from coding_harness.security import commands

    command = args.get("command")
    if not isinstance(command, str) or not command:
        return False
    return commands.classify(command, Autonomy.OFF, cwd=cwd).action != commands.ALLOW


def run_gate(gate: Gate, cwd: str, remaining_s: float | None) -> CheckReport:
    """One green-before-done run compared against the baseline; updates the gate."""
    after = run_checks(gate.cmds, cwd, _budget(GATE_CAP_S, 0.4, remaining_s))
    result = compare(gate.before, after)
    gate.passed, gate.report = result.passed, result.report
    return result


def targeted_report(edited_paths: list[str], cwd: str, remaining_s: float | None) -> str | None:
    """Failures from the tests this step's edits target, phrased for the model; None when green or none found."""
    files, note = targeted_tests(edited_paths, cwd)
    if not files:
        return None
    cmd = shlex.join(pytest_cmd(cwd) + files)
    result = run_checks([cmd], cwd, _budget(TARGETED_CAP_S, 1.0, remaining_s))
    if result.passed is not False:
        return None
    head = "Tests covering your edit failed. Fix the cause, then continue:"
    if note:
        head += f"\n({note})"
    return head + "\n\n" + result.report
