"""Unit tests for the stuck-call detector (core/stuck.py)."""
from __future__ import annotations

import os
import unittest
from unittest import mock

from coding_harness.core import stuck


def _grep(pattern: str = "needle") -> tuple[str, str]:
    return ("Grep", stuck.canonical("Grep", {"pattern": pattern, "path": "/repo"}))


class TestCanonical(unittest.TestCase):
    def test_key_order_does_not_matter(self) -> None:
        a = stuck.canonical("Grep", {"pattern": "x", "path": "/p"})
        b = stuck.canonical("Grep", {"path": "/p", "pattern": "x"})
        self.assertEqual(a, b)

    def test_whitespace_runs_normalize(self) -> None:
        a = stuck.canonical("Bash", {"command": "ls   -la \n  /tmp "})
        b = stuck.canonical("Bash", {"command": "ls -la /tmp"})
        self.assertEqual(a, b)

    def test_nested_values_normalize(self) -> None:
        a = stuck.canonical("T", {"items": ["a  b", {"k": " v "}]})
        b = stuck.canonical("T", {"items": ["a b", {"k": "v"}]})
        self.assertEqual(a, b)

    def test_name_and_values_distinguish(self) -> None:
        self.assertNotEqual(
            stuck.canonical("Grep", {"pattern": "x"}),
            stuck.canonical("Read", {"pattern": "x"}),
        )
        self.assertNotEqual(
            stuck.canonical("Grep", {"pattern": "x"}),
            stuck.canonical("Grep", {"pattern": "y"}),
        )


class TestStepTracker(unittest.TestCase):
    def test_two_identical_steps_nudge(self) -> None:
        t = stuck.StepTracker()
        self.assertIsNone(t.observe([_grep()], [False], False))
        self.assertEqual(t.observe([_grep()], [False], False), "nudge")

    def test_four_identical_steps_halt(self) -> None:
        t = stuck.StepTracker()
        verdicts = [t.observe([_grep()], [False], False) for _ in range(4)]
        self.assertEqual(verdicts, [None, "nudge", "nudge", "halt"])

    def test_successful_edit_between_resets(self) -> None:
        t = stuck.StepTracker()
        edit = ("Edit", stuck.canonical("Edit", {"file_path": "/f", "old_string": "a"}))
        self.assertIsNone(t.observe([_grep()], [False], False))
        self.assertIsNone(t.observe([edit], [False], True))
        self.assertIsNone(t.observe([_grep()], [False], False))
        self.assertEqual(t.run, 1)

    def test_repeated_mutating_step_never_counts(self) -> None:
        # Grep, Edit, Grep passes with the flag ignored: the Edit is a different
        # key and breaks the run by itself. Only a repeated mutating step
        # tells reset-on-mutation apart from no reset at all.
        t = stuck.StepTracker()
        edit = ("Edit", stuck.canonical("Edit", {"file_path": "/f", "old_string": "a"}))
        self.assertEqual([t.observe([edit], [False], True) for _ in range(4)], [None] * 4)
        self.assertEqual(t.run, 0)

    def test_different_call_breaks_the_run(self) -> None:
        t = stuck.StepTracker()
        t.observe([_grep()], [False], False)
        self.assertIsNone(t.observe([_grep("other")], [False], False))
        self.assertEqual(t.observe([_grep("other")], [False], False), "nudge")

    def test_failed_then_succeeded_call_is_not_a_repeat(self) -> None:
        t = stuck.StepTracker()
        t.observe([_grep()], [True], False)
        self.assertIsNone(t.observe([_grep()], [False], False))

    def test_multi_call_step_repeats_only_when_every_call_repeats(self) -> None:
        t = stuck.StepTracker()
        t.observe([_grep(), _grep("b")], [False, False], False)
        self.assertEqual(t.observe([_grep()], [False], False), "nudge")
        self.assertIsNone(t.observe([_grep(), _grep("new")], [False, False], False))

    def test_kill_switch(self) -> None:
        with mock.patch.dict(os.environ, {"HARNESS_STUCK_DETECT": "0"}):
            t = stuck.StepTracker()
            verdicts = [t.observe([_grep()], [False], False) for _ in range(5)]
        self.assertEqual(verdicts, [None] * 5)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
