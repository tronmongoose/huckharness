"""Use-case buckets that group skills for the GUI's Skills tab.

Exports ``BUCKETS``, ``OTHER``, ``USER_MAP``, ``canonical_bucket``,
``user_buckets``, ``bucket_of`` and ``bucket_counts``. A flat list of forty
skills is a wall; a handful of use-case tiles is a menu. A ``category:``
frontmatter key places a skill. Skills without one can be placed by name in
``~/.config/bjorn/skill_buckets.json``, a map of bucket to skill names that
lives with the user's skills rather than in this package, because skill names
describe the person who wrote them. Anything unmatched lands in ``OTHER``.
The system prompt does not see buckets; they are display-only.
"""
from __future__ import annotations

import json
from pathlib import Path

BUCKETS = (
    "Money & household",
    "Briefs & digests",
    "Inbox & comms",
    "Writing & publishing",
    "Design & media",
    "Fleet & ops",
    "Security & guardrails",
)
OTHER = "Other"
USER_MAP = Path("~/.config/bjorn/skill_buckets.json")
_BY_LOWER = {b.lower(): b for b in BUCKETS}


def canonical_bucket(category: str) -> str | None:
    """The bucket ``category`` names, case-insensitively; None when it names none."""
    return _BY_LOWER.get(category.strip().lower())


def user_buckets() -> dict[str, str]:
    """Skill name to bucket from USER_MAP; empty when it is absent or malformed."""
    try:
        raw = json.loads(USER_MAP.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for bucket, names in raw.items():
        canon = canonical_bucket(str(bucket))
        if canon and isinstance(names, list):
            out.update({str(n): canon for n in names})
    return out


def bucket_of(name: str, category: str = "", by_name: dict[str, str] | None = None) -> str:
    """The bucket for a skill: a valid ``category`` first, then the user's map, then Other."""
    by_name = user_buckets() if by_name is None else by_name
    return canonical_bucket(category) or by_name.get(name, OTHER)


def bucket_counts(buckets: list[str]) -> list[dict[str, object]]:
    """Every named bucket in order with its count, plus Other last when it has any."""
    rows: list[dict[str, object]] = [{"name": b, "count": buckets.count(b)} for b in BUCKETS]
    other = buckets.count(OTHER)
    if other:
        rows.append({"name": OTHER, "count": other})
    return rows
