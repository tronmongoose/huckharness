"""Reviewer agreement scoring and the rule that decides when a local reviewer
may replace the frontier one.

One definition, two consumers: eval/review_bench.py scores diff-only replays
here, and the deployment's shadow-mode scorer scores live verdict pairs here.
A second copy would let the bench and the thing that acts on the bench drift.

Exports: score, switch_ok, SwitchRule, DEFAULT_RULE.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


def score(verdicts: list[dict[str, Any]]) -> dict[str, Any]:
    """Agreement split by class; PASS rows are few, so both numbers are shown.

    Each verdict is {"opus": "PASS"|"FAIL", "local": same or None, "seconds": float}.
    ``always_fail_agreement`` is the score a reviewer gets for answering FAIL to
    everything — on a FAIL-heavy corpus it is high, and it is the number a real
    reviewer has to beat before its agreement means anything.
    """
    fails = [v for v in verdicts if v["opus"] == "FAIL"]
    passes = [v for v in verdicts if v["opus"] == "PASS"]
    answered = [v for v in verdicts if v.get("local") is not None]
    agree = sum(1 for v in answered if v["local"] == v["opus"])
    fail_recall = sum(1 for v in fails if v.get("local") == "FAIL") / max(len(fails), 1)
    false_fail = sum(1 for v in passes if v.get("local") == "FAIL") / max(len(passes), 1)
    secs = sorted(float(v.get("seconds") or 0.0) for v in verdicts) or [0.0]
    return {
        "rows": len(verdicts), "answered": len(answered),
        "answered_rate": round(len(answered) / max(len(verdicts), 1), 3),
        "agreement": round(agree / max(len(answered), 1), 3),
        "fail_recall": round(fail_recall, 3), "n_fail": len(fails),
        "false_fail_rate": round(false_fail, 3), "n_pass": len(passes),
        "always_fail_agreement": round(len(fails) / max(len(verdicts), 1), 3),
        "median_seconds": secs[len(secs) // 2],
    }


@dataclass(frozen=True)
class SwitchRule:
    """Thresholds a local reviewer must clear to become authoritative."""

    min_agreement: float = 0.8
    max_false_fail: float = 0.2
    min_pass_labels: int = 20
    min_answered_rate: float = 0.9
    min_margin_over_constant: float = 0.0


DEFAULT_RULE = SwitchRule()


def switch_ok(s: dict[str, Any], rule: SwitchRule = DEFAULT_RULE) -> tuple[bool, list[str]]:
    """Whether ``score(...)`` clears ``rule``; always returns why not.

    Every clause exists because a real model passed without it. gpt-oss:20b
    scored 1.0 agreement on the 16 of 53 rows it managed to answer, so the
    answered-rate floor comes first. A constant-FAIL reviewer scored 0.906 on
    the same corpus, so agreement is measured against that, not against zero.
    """
    reasons: list[str] = []
    if s["answered_rate"] < rule.min_answered_rate:
        reasons.append(f"answered {s['answered_rate']:.0%} of rows, "
                       f"need {rule.min_answered_rate:.0%} — a reviewer that "
                       "declines to answer is not a reviewer")
    if s["n_pass"] < rule.min_pass_labels:
        reasons.append(f"{s['n_pass']} PASS labels, need {rule.min_pass_labels} — "
                       "agreement on a FAIL-only corpus cannot distinguish "
                       "judgment from a rubber stamp")
    if s["agreement"] < rule.min_agreement:
        reasons.append(f"agreement {s['agreement']} < {rule.min_agreement}")
    floor = s["always_fail_agreement"] + rule.min_margin_over_constant
    if s["agreement"] <= floor:
        reasons.append(f"agreement {s['agreement']} does not beat the "
                       f"constant-FAIL baseline {floor}")
    if s["false_fail_rate"] > rule.max_false_fail:
        reasons.append(f"false-FAIL {s['false_fail_rate']} > {rule.max_false_fail}")
    return (not reasons), reasons
