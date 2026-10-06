"""Match ladder for the Edit tool: locate old_string with graded whitespace tolerance.

Exports ``Match``, ``locate`` (tiers exact, rstrip, indent, each requiring a
unique hit), ``TIERS``, ``enabled_tiers``, and ``nearest_window`` (the fuzzy
hint used when nothing matched).
"""
from __future__ import annotations

import difflib
import os
import textwrap
from dataclasses import dataclass
from typing import Callable

NEAREST_MIN_RATIO = 0.6
NEAREST_MAX_LINES = 5000
TIERS = ("exact", "rstrip", "indent")


@dataclass
class Match:
    """A unique hit: char span in the text, the tier that found it, and the text to insert."""

    start: int
    end: int
    tier: str
    replacement: str


def enabled_tiers() -> tuple[str, ...]:
    """Active tiers; HARNESS_EDIT_TIERS=exact turns the tolerant ones off."""
    if os.environ.get("HARNESS_EDIT_TIERS") == "exact":
        return TIERS[:1]
    return TIERS


def locate(text: str, old: str, new: str) -> Match | list[str]:
    """Unique Match at the first tier that yields exactly one hit, else the tiers that were ambiguous."""
    ambiguous: list[str] = []
    for tier in enabled_tiers():
        spans = _exact_spans(text, old) if tier == "exact" else _window_spans(text, old, _NORMS[tier])
        if len(spans) == 1:
            start, end = spans[0]
            replacement = new
            if tier == "indent":
                replacement = _reindent(new, _margin(old), _margin(text[start:end]))
            return Match(start, end, tier, replacement)
        if spans:
            ambiguous.append(tier)
    return ambiguous


def _exact_spans(text: str, old: str) -> list[tuple[int, int]]:
    """Non-overlapping exact occurrences, the same set str.replace would touch."""
    if not old:
        return []
    spans = []
    idx = text.find(old)
    while idx >= 0:
        spans.append((idx, idx + len(old)))
        idx = text.find(old, idx + len(old))
    return spans


def _rstrip_lines(block: str) -> list[str]:
    """Lines with trailing whitespace removed."""
    return [line.rstrip() for line in block.splitlines()]


def _dedent_lines(block: str) -> list[str]:
    """Lines with the common indent and trailing whitespace removed."""
    return _rstrip_lines(textwrap.dedent(block))


_NORMS: dict[str, Callable[[str], list[str]]] = {"rstrip": _rstrip_lines, "indent": _dedent_lines}


def _window_spans(text: str, old: str, norm: Callable[[str], list[str]]) -> list[tuple[int, int]]:
    """Char spans of every whole-line window that equals old under norm."""
    lines = text.splitlines(keepends=True)
    want = norm(old)
    n = len(want)
    if n == 0 or n > len(lines):
        return []
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line))
    keep_newline = old.endswith("\n")
    spans = []
    for i in range(len(lines) - n + 1):
        window = "".join(lines[i:i + n])
        if norm(window) != want:
            continue
        end = starts[i + n]
        if not keep_newline:
            end -= len(lines[i + n - 1]) - len(lines[i + n - 1].rstrip("\r\n"))
        spans.append((starts[i], end))
    return spans


def _margin(block: str) -> str:
    """Leading whitespace shared by every non-blank line of block."""
    lines = [line for line in block.splitlines() if line.strip()]
    if not lines:
        return ""
    prefix = os.path.commonprefix(lines)
    return prefix[:len(prefix) - len(prefix.lstrip())]


def _reindent(new: str, old_prefix: str, win_prefix: str) -> str:
    """Shift new by the indent delta between old_string and the matched window."""
    out = []
    for line in new.splitlines(keepends=True):
        if not line.strip():
            out.append(line)
            continue
        ws = len(line) - len(line.lstrip())
        out.append(win_prefix + line[min(ws, len(old_prefix)):])
    return "".join(out)


def nearest_window(text: str, old: str) -> tuple[int, int, float] | None:
    """Best fuzzy line window for old in text as (start, end, ratio), 1-based, or None below threshold."""
    lines = text.splitlines()[:NEAREST_MAX_LINES]
    if not lines:
        return None
    n = max(1, len(old.splitlines()))
    # old is seq2 so difflib caches its index once instead of per window. autojunk
    # is off because past 200 chars it marks common characters junk and the
    # best-scoring window drifts off the real region.
    matcher = difflib.SequenceMatcher(None, "", old, autojunk=False)
    best: tuple[int, int, float] | None = None
    floor = NEAREST_MIN_RATIO
    for start in range(max(1, len(lines) - n + 1)):
        chunk = lines[start:start + n]
        matcher.set_seq1("\n".join(chunk))
        if matcher.real_quick_ratio() < floor or matcher.quick_ratio() < floor:
            continue
        ratio = matcher.ratio()
        if ratio >= floor and (best is None or ratio > best[2]):
            best = (start + 1, start + len(chunk), ratio)
            floor = ratio
    return best

