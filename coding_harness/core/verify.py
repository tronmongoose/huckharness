"""Deterministic post-edit verify gate for the in-loop repair cycle (P0-2, P1-4).

After an Edit/Write, the session runs cheap local checks on the touched files
and feeds any failure back to the model as the next observation, so malformed
edits get repaired in-loop instead of surfacing at PR time. Python files get
py_compile plus ruff, which first applies its safe autofixes (import order,
unused imports) so only the residual reaches the model as one line per
finding; every file the autofix rewrote is named in ``notes`` so the model
re-reads it before its next Edit instead of matching stale text. Every other
suffix goes through core.verifiers; its SKIPs (missing
tool, no verifier) are logged rather than reported so a vacuous pass stays
visible without spending the model's attention. Checks are bounded,
network-free, and do NOT route through the tool registry: the harness's own
verify commands must not trip Sentinel review. Tooling absence fails open.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

from coding_harness.core import verifiers

_TIMEOUT_S = 30
_MAX_REPORT_CHARS = 2000  # keep the fed-back observation small for the local model
_RULES = ("--select", "F,I,UP,B,E,W", "--ignore", "E501")
_RUFF_INSTRUCTION = (
    "ruff (fix each with a minimal Edit at that line; do not rewrite the file):"
)
REFORMAT_NOTE = "ruff reformatted {path}: re-read before your next Edit"
_log = logging.getLogger(__name__)


def _resolve_ruff() -> str | None:
    """ruff next to the running interpreter (the venv the harness runs under),
    then anything on PATH. None if unavailable — the caller skips lint."""
    sibling = Path(sys.executable).parent / "ruff"
    if sibling.is_file():
        return str(sibling)
    return shutil.which("ruff")


def _findings(stdout: str) -> list[str]:
    """ruff's JSON findings as 'path:line: CODE message' lines."""
    return [
        f"{d['filename']}:{d['location']['row']}: {d['code']} {d['message']}"
        for d in json.loads(stdout)
    ]


def _autofix(ruff: str, files: list[str]) -> list[str]:
    """Apply ruff's safe fixes; the files whose bytes changed, for the re-read note."""
    before = {f: Path(f).read_bytes() for f in files}
    subprocess.run(
        [ruff, "check", "--fix-only", *_RULES, *files],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
    )
    return [f for f in files if Path(f).read_bytes() != before[f]]


def _ruff(files: list[str], notes: list[str]) -> list[str]:
    """Autofix what ruff can (noting each rewritten file), then report the residual. No-op if ruff absent."""
    ruff = _resolve_ruff()
    if ruff is None or not files:
        return []
    notes += [REFORMAT_NOTE.format(path=f) for f in _autofix(ruff, files)]
    proc = subprocess.run(
        [ruff, "check", "--output-format", "json", *_RULES, *files],
        capture_output=True, text=True, timeout=_TIMEOUT_S,
    )
    if proc.returncode == 0:
        return []
    try:
        lines = _findings(proc.stdout)
    except ValueError:
        # ruff itself failed (exit 2): the raw text is all there is to show.
        out = (proc.stdout or proc.stderr).strip()
        return [f"ruff:\n{out}"] if out else []
    return [_RUFF_INSTRUCTION + "\n" + "\n".join(lines)] if lines else []


def verify_files(paths: list[str], *, notes: list[str] | None = None) -> str | None:
    """Return a repair report for the given files, or None if all pass.

    Only existing files are checked. The report is the observation fed back
    to the model, so it names the failing check and its output verbatim.
    ``notes`` collects the one-line re-read notices for files the ruff
    autofix rewrote; they are not failures, so they never make a report.
    """
    existing = sorted({p for p in paths if Path(p).is_file()})
    if not existing:
        return None
    checks = verifiers.verify_paths(existing, os.getcwd())
    for check in checks:
        if check.status == "SKIP":
            _log.info("verify skip: %s: %s", check.name, check.detail)
    problems = [f"{c.name}:\n{c.detail}" for c in checks if c.status == "FAIL"]
    problems += _ruff([p for p in existing if p.endswith(".py")], [] if notes is None else notes)
    if not problems:
        return None
    body = "\n\n".join(problems)[:_MAX_REPORT_CHARS]
    return (
        "Your edit did not pass the local verify gate. Fix the file(s) below, "
        "then continue — do not restate the errors:\n\n" + body
    )
