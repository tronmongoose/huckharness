"""Per-session metrics for the eval runner (P0-4).

Reduces one harness session's transcript and the per-run audit chain to the
counts the eval report compares across runs: tool and edit calls, edit errors,
Bash errors split into harness-caused waste and informative failures, repair
and review rounds, the halt reason, and token and inner-step sums from the
audit's turn rows. Missing or unreadable inputs read as zero, never raise.

Exports: session_metrics, transcript_metrics, audit_metrics.
"""
from __future__ import annotations

import json
from pathlib import Path

EDIT_TOOLS = ("Edit", "Write")
HARNESS_CAUSED = "harness_caused"
AUDIT_SUMS = ("tokens_in", "tokens_out", "inner_steps")


def _records(path: Path) -> list[dict]:
    """Transcript records via the harness's own tolerant JSONL reader."""
    from coding_harness.core.replay import read_records
    return [r for r in read_records(path) if isinstance(r, dict)]


def _rounds(records: list[dict], kind: str) -> int:
    """Highest round number logged for a kind; the session logs each round twice."""
    return max((int(r.get("round") or 0) for r in records if r.get("kind") == kind),
               default=0)


def _halted_reason(records: list[dict]) -> str | None:
    """turn_done carries the reason; a bare deadline or error record implies it."""
    for r in records:
        if r.get("kind") == "turn_done" and r.get("halted_reason"):
            return str(r["halted_reason"])
    for r in records:
        if r.get("kind") in ("deadline", "error"):
            return str(r["kind"])
    return None


def transcript_metrics(session_log: Path) -> dict:
    """Tool-call, error, repair and review counts from one session transcript."""
    records = _records(session_log)
    results = [r for r in records if r.get("kind") == "tool_result"]
    edits = [r for r in results if r.get("name") in EDIT_TOOLS]
    bash = [r for r in results if r.get("name") == "Bash"]
    bash_errors = [r for r in bash if r.get("is_error")]
    harness = [r for r in bash_errors
               if str(r.get("error_class") or "").startswith(HARNESS_CAUSED)]
    return {
        "tool_calls": len(results),
        "edit_calls": len(edits),
        "edit_errors": sum(1 for r in edits if r.get("is_error")),
        "bash_calls": len(bash),
        "bash_errors_informative": len(bash_errors) - len(harness),
        "bash_errors_harness": len(harness),
        "repair_rounds": _rounds(records, "verify_repair"),
        "review_rounds": _rounds(records, "agentic_review"),
        "halted_reason": _halted_reason(records),
    }


def audit_metrics(meta_dir: Path) -> dict:
    """Token and inner-step sums over the turn rows of meta_dir/audit.jsonl."""
    out = dict.fromkeys(AUDIT_SUMS, 0)
    try:
        lines = (meta_dir / "audit.jsonl").read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict) or row.get("kind") != "turn":
            continue
        for key in AUDIT_SUMS:
            out[key] += int(row.get(key) or 0)
    return out


def session_metrics(session_log: Path, meta_dir: Path) -> dict:
    """Transcript counts plus audit sums for one eval run; never raises."""
    return {**transcript_metrics(session_log), **audit_metrics(meta_dir)}
