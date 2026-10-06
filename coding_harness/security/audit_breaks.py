"""Out-of-band ledger of acknowledged audit-chain breaks.

A hash chain that is edited until it verifies is no longer evidence of
anything. So a break is never repaired in place: the audit file keeps its
exact bytes, and an operator records the break here instead. The verifier
consults this ledger and resumes past a break it recognises, reporting it
rather than hiding it.

An acknowledgement is pinned to four things — the line, the hash the chain
expected, the hash the entry carries, and a sha256 over every byte before that
line. Altering any earlier entry changes the prefix digest, the acknowledgement
stops matching, and the chain goes red again. So an ack can excuse the one
discontinuity it names and cannot launder a later rewrite of history.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from coding_harness.core.paths import meta_dir

BREAKS_PATH = meta_dir() / "audit-breaks.jsonl"

# The identity of a break. An ack must reproduce all four to apply.
PINNED_FIELDS = ("line", "expected_prev", "observed_prev", "observed_seq", "prefix_sha256")


def load(path: Path | None = None) -> dict[int, dict[str, Any]]:
    """Acknowledged breaks keyed by line number. Empty when the ledger is absent."""
    p = path or BREAKS_PATH
    out: dict[int, dict[str, Any]] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record_ = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record_, dict) and isinstance(record_.get("line"), int):
            out[record_["line"]] = record_
    return out


def matches(ack: dict[str, Any], break_info: dict[str, Any]) -> bool:
    """True when ``ack`` names exactly this break, all pinned fields equal."""
    return all(ack.get(field) == break_info.get(field) for field in PINNED_FIELDS)


def record(
    break_info: dict[str, Any],
    *,
    reason: str,
    acknowledged_by: str,
    path: Path | None = None,
) -> dict[str, Any]:
    """Append an acknowledgement for ``break_info``. Returns the written record."""
    p = path or BREAKS_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "v": 1,
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        **{field: break_info.get(field) for field in PINNED_FIELDS},
        "reason": reason,
        "acknowledged_by": acknowledged_by,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry
