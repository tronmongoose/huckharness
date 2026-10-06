"""The local-reviewer switch rule, tested against the real bench corpus.

Every case here replays a verdict distribution a real model actually produced
on the 2026-08-30 reviewer bench (53 labelled PRs, 48 FAIL / 5 PASS). A rule
that only passes invented data would not have caught gpt-oss:20b, which scored
1.0 agreement by answering 16 rows and declining 37.
"""
from __future__ import annotations

import unittest

from coding_harness.core.review_score import DEFAULT_RULE, SwitchRule, score, switch_ok


def rows(n_fail: int, n_pass: int, local_for_fail, local_for_pass) -> list[dict]:
    """A verdict list with the given opus split and local answers."""
    out = [{"opus": "FAIL", "local": local_for_fail, "seconds": 1.0} for _ in range(n_fail)]
    out += [{"opus": "PASS", "local": local_for_pass, "seconds": 1.0} for _ in range(n_pass)]
    return out


class ScoreTests(unittest.TestCase):
    def test_constant_fail_reviewer_scores_the_corpus_skew(self):
        """Answering FAIL to everything scores 0.906 on the real corpus."""
        s = score(rows(48, 5, "FAIL", "FAIL"))
        self.assertEqual(s["agreement"], 0.906)
        self.assertEqual(s["always_fail_agreement"], 0.906)
        self.assertEqual(s["false_fail_rate"], 1.0)

    def test_unanswered_rows_do_not_count_as_agreement(self):
        s = score(rows(48, 5, None, None))
        self.assertEqual(s["answered"], 0)
        self.assertEqual(s["answered_rate"], 0.0)

    def test_median_seconds_survives_missing_timings(self):
        s = score([{"opus": "FAIL", "local": "FAIL"}])
        self.assertEqual(s["median_seconds"], 0.0)


class SwitchRuleTests(unittest.TestCase):
    def test_constant_fail_reviewer_is_rejected(self):
        """The whole point: high agreement from skew is not judgment."""
        ok, reasons = switch_ok(score(rows(48, 5, "FAIL", "FAIL")))
        self.assertFalse(ok)
        self.assertTrue(any("constant-FAIL baseline" in r for r in reasons))
        self.assertTrue(any("false-FAIL" in r for r in reasons))

    def test_high_agreement_on_a_declined_majority_is_rejected(self):
        """gpt-oss:20b: 1.0 agreement, 16 of 53 rows answered."""
        verdicts = rows(48, 5, None, None)
        for v in verdicts[:16]:
            v["local"] = "FAIL"
        s = score(verdicts)
        self.assertEqual(s["agreement"], 1.0)
        ok, reasons = switch_ok(s)
        self.assertFalse(ok)
        self.assertTrue(any("declines to answer" in r for r in reasons))

    def test_mistral_2026_08_30_is_rejected_for_too_few_pass_labels(self):
        """0.811 agreement, 0 false-FAIL — still not enough PASS rows to judge."""
        verdicts = rows(48, 5, "FAIL", "PASS")
        for v in verdicts[:10]:
            v["local"] = "PASS"          # the 10 FAILs it missed
        s = score(verdicts)
        self.assertEqual(s["n_pass"], 5)
        ok, reasons = switch_ok(s)
        self.assertFalse(ok)
        self.assertTrue(any("PASS labels" in r for r in reasons))

    def test_a_real_reviewer_on_a_balanced_corpus_passes(self):
        """40 FAIL / 25 PASS, one miss each way: clears every clause."""
        verdicts = rows(40, 25, "FAIL", "PASS")
        verdicts[0]["local"] = "PASS"
        verdicts[-1]["local"] = "FAIL"
        s = score(verdicts)
        ok, reasons = switch_ok(s)
        self.assertTrue(ok, reasons)
        self.assertGreater(s["agreement"], s["always_fail_agreement"])

    def test_agreement_must_beat_the_baseline_not_merely_the_floor(self):
        """A reviewer scoring exactly the constant-FAIL number is not an improvement.

        30 FAIL answered PASS, 30 PASS answered PASS: 30 of 60 correct, which is
        the same 0.5 a constant-FAIL reviewer gets. Thresholds are loosened so
        only the baseline clause can fire.
        """
        verdicts = rows(30, 30, "PASS", "PASS")
        s = score(verdicts)
        self.assertEqual(s["agreement"], s["always_fail_agreement"])
        ok, reasons = switch_ok(s, SwitchRule(min_agreement=0.4, min_pass_labels=5,
                                              max_false_fail=1.0))
        self.assertFalse(ok)
        self.assertEqual(len(reasons), 1)
        self.assertIn("constant-FAIL baseline", reasons[0])

    def test_default_rule_thresholds_are_the_documented_ones(self):
        self.assertEqual(DEFAULT_RULE.min_agreement, 0.8)
        self.assertEqual(DEFAULT_RULE.max_false_fail, 0.2)
        self.assertEqual(DEFAULT_RULE.min_pass_labels, 20)
        self.assertEqual(DEFAULT_RULE.min_answered_rate, 0.9)


if __name__ == "__main__":
    unittest.main()
