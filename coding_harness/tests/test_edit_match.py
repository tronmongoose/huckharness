"""Tests for the Edit tolerance ladder (edit_match.locate), multi-edit, and replace_all guards.

Real filesystem in a temp directory, no mocks beyond os.environ patches.
"""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.tools.edit import Edit
from coding_harness.tools.edit_match import Match, locate

FILE = (
    "def outer():\n"
    "    if flag:\n"
    "        x = 1\n"
    "        y = 2\n"
    "    return x\n"
)


class TestLocate(unittest.TestCase):

    def test_exact_tier(self):
        m = locate(FILE, "    return x\n", "    return y\n")
        self.assertIsInstance(m, Match)
        self.assertEqual(m.tier, "exact")
        self.assertEqual(FILE[m.start:m.end], "    return x\n")
        self.assertEqual(m.replacement, "    return y\n")

    def test_rstrip_tier_tolerates_trailing_whitespace_in_file(self):
        text = "a = 1   \nb = 2\t\nc = 3\n"
        m = locate(text, "a = 1\nb = 2\n", "a = 10\nb = 20\n")
        self.assertIsInstance(m, Match)
        self.assertEqual(m.tier, "rstrip")
        self.assertEqual(text[m.start:m.end], "a = 1   \nb = 2\t\n")
        self.assertEqual(m.replacement, "a = 10\nb = 20\n")

    def test_rstrip_tier_tolerates_trailing_whitespace_in_old(self):
        m = locate(FILE, "        x = 1  \n        y = 2 \n", "        z = 3\n")
        self.assertIsInstance(m, Match)
        self.assertEqual(m.tier, "rstrip")
        self.assertEqual(FILE[m.start:m.end], "        x = 1\n        y = 2\n")

    def test_indent_tier_plus_four_reindents_replacement(self):
        m = locate(FILE, "    x = 1\n    y = 2\n", "    x = 10\n\n    y = 20\n")
        self.assertIsInstance(m, Match)
        self.assertEqual(m.tier, "indent")
        self.assertEqual(FILE[m.start:m.end], "        x = 1\n        y = 2\n")
        self.assertEqual(m.replacement, "        x = 10\n\n        y = 20\n")

    def test_indent_tier_minus_four_reindents_replacement(self):
        m = locate(FILE, "            x = 1\n            y = 2\n", "            x = 10\n                y = 20\n")
        self.assertIsInstance(m, Match)
        self.assertEqual(m.tier, "indent")
        self.assertEqual(m.replacement, "        x = 10\n            y = 20\n")

    def test_indent_tier_keeps_relative_indentation(self):
        # Stripped lines alone would match; dedent keeps the if/body shape intact.
        self.assertEqual(locate(FILE, "if flag:\nx = 1\n", "pass\n"), [])

    def test_ambiguous_at_rstrip_reports_tiers(self):
        text = "a = 1 \nb\na = 1\t\n"
        self.assertEqual(locate(text, "a = 1\n", "a = 2\n"), ["rstrip", "indent"])

    def test_ambiguous_exact_partial_line(self):
        self.assertEqual(locate(FILE, "x", "z"), ["exact"])

    def test_no_match_returns_empty_list(self):
        self.assertEqual(locate(FILE, "nothing here\n", "x\n"), [])

    def test_empty_old_returns_empty_list(self):
        self.assertEqual(locate(FILE, "", "x"), [])

    def test_kill_switch_exact_only(self):
        with patch.dict(os.environ, {"HARNESS_EDIT_TIERS": "exact"}):
            self.assertEqual(locate(FILE, "    x = 1\n    y = 2\n", "    x = 2\n"), [])
        self.assertIsInstance(locate(FILE, "    x = 1\n    y = 2\n", "    x = 2\n"), Match)

    def test_old_without_trailing_newline_leaves_newline(self):
        m = locate(FILE, "    x = 1\n    y = 2", "    x = 3")
        self.assertIsInstance(m, Match)
        self.assertEqual(FILE[m.end:], "\n    return x\n")


class TestEditLadder(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tool = Edit()
        self.fixture = os.path.join(self.tmpdir, "code.py")
        Path(self.fixture).write_text(FILE)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _edit(self, **args):
        return self.tool.run({"file_path": self.fixture, **args})

    def test_exact_tier_telemetry(self):
        plan = self.tool.plan({"file_path": self.fixture, "old_string": "x = 1", "new_string": "x = 9"})
        self.assertEqual(plan.metadata["match_tier"], "exact")
        result = self.tool.apply(plan)
        self.assertEqual(result.metadata["match_tier"], "exact")
        self.assertNotIn("tolerance", result.content)

    def test_rstrip_tier_mentioned_in_result(self):
        Path(self.fixture).write_text("a = 1  \nb = 2\n")
        result = self._edit(old_string="a = 1\n", new_string="a = 5\n")
        self.assertFalse(result.is_error, result.content)
        self.assertIn("matched with whitespace tolerance (tier rstrip)", result.content)
        self.assertEqual(result.metadata["match_tier"], "rstrip")
        self.assertEqual(Path(self.fixture).read_text(), "a = 5\nb = 2\n")

    def test_indent_tier_writes_reindented_replacement(self):
        result = self._edit(old_string="    x = 1\n    y = 2\n", new_string="    x = 10\n    y = 20\n")
        self.assertFalse(result.is_error, result.content)
        self.assertIn("(tier indent)", result.content)
        self.assertEqual(
            Path(self.fixture).read_text(),
            "def outer():\n    if flag:\n        x = 10\n        y = 20\n    return x\n",
        )

    def test_ambiguous_at_tolerant_tier_rejected_with_reason(self):
        Path(self.fixture).write_text("a = 1 \nb\na = 1\t\n")
        result = self._edit(old_string="a = 1\n", new_string="a = 2\n")
        self.assertTrue(result.is_error)
        self.assertIn("more than one region under whitespace tolerance (tiers rstrip, indent)", result.content)
        self.assertEqual(Path(self.fixture).read_text(), "a = 1 \nb\na = 1\t\n")

    def test_no_match_falls_to_nearest_hint(self):
        result = self._edit(old_string="    if flag:\n        x = 11\n", new_string="pass\n")
        self.assertTrue(result.is_error)
        self.assertIn("closest match at lines 2-3", result.content)

    def test_empty_old_string_rejected(self):
        result = self._edit(old_string="", new_string="x")
        self.assertTrue(result.is_error)
        self.assertIn("old_string must not be empty", result.content)
        self.assertEqual(Path(self.fixture).read_text(), FILE)

    def test_kill_switch_disables_tolerant_tiers(self):
        with patch.dict(os.environ, {"HARNESS_EDIT_TIERS": "exact"}):
            result = self._edit(old_string="    x = 1\n    y = 2\n", new_string="    x = 2\n")
        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content)
        self.assertEqual(Path(self.fixture).read_text(), FILE)


class TestMultiEdit(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tool = Edit()
        self.fixture = os.path.join(self.tmpdir, "code.py")
        Path(self.fixture).write_text(FILE)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_multi_edit_success_one_diff(self):
        plan = self.tool.plan({
            "file_path": self.fixture,
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 10"},
                {"old_string": "return x", "new_string": "return x + y"},
            ],
        })
        self.assertEqual(plan.metadata["replacements"], 2)
        self.assertIn("+        x = 10", plan.unified_diff)
        self.assertIn("+    return x + y", plan.unified_diff)
        result = self.tool.apply(plan)
        self.assertIn("2 replacements", result.content)
        self.assertIn("x = 10", Path(self.fixture).read_text())
        self.assertIn("return x + y", Path(self.fixture).read_text())

    def test_multi_edit_sees_earlier_edits(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 10"},
                {"old_string": "x = 10", "new_string": "x = 100"},
            ],
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("x = 100", Path(self.fixture).read_text())

    def test_multi_edit_atomic_failure(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 10"},
                {"old_string": "missing", "new_string": "anything"},
            ],
        })
        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content)
        self.assertEqual(Path(self.fixture).read_text(), FILE)

    def test_multi_edit_labels_tolerant_edit(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "edits": [
                {"old_string": "x = 1", "new_string": "x = 10"},
                {"old_string": "    x = 10\n    y = 2\n", "new_string": "    x = 10\n    y = 20\n"},
            ],
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("edit 2 matched with whitespace tolerance (tier indent)", result.content)
        self.assertEqual(result.metadata["match_tier"], "indent")

    def test_both_forms_rejected(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "x = 1",
            "new_string": "x = 2",
            "edits": [{"old_string": "x = 1", "new_string": "x = 2"}],
        })
        self.assertTrue(result.is_error)
        self.assertIn("not both", result.content)

    def test_neither_form_rejected(self):
        result = self.tool.run({"file_path": self.fixture})
        self.assertTrue(result.is_error)
        self.assertIn("required", result.content)

    def test_malformed_edits_rejected(self):
        result = self.tool.run({"file_path": self.fixture, "edits": [{"old_string": "x"}]})
        self.assertTrue(result.is_error)
        self.assertIn("edits[1]", result.content)
        self.assertTrue(self.tool.run({"file_path": self.fixture, "edits": []}).is_error)

    def test_schema_requires_only_file_path(self):
        self.assertEqual(Edit.parameters["required"], ["file_path"])
        self.assertIn("edits", Edit.parameters["properties"])


class TestReplaceAllGuards(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tool = Edit()
        self.fixture = os.path.join(self.tmpdir, "arith.py")
        Path(self.fixture).write_text("".join(f"v{i} = {i} + 1\n" for i in range(65)))

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_short_old_string_rejected(self):
        result = self.tool.run({
            "file_path": self.fixture, "old_string": " ", "new_string": "\n", "replace_all": True,
        })
        self.assertTrue(result.is_error)
        self.assertIn("old_string too short for replace_all; give at least 3 non-space characters", result.content)

    def test_too_many_replacements_rejected_with_count(self):
        result = self.tool.run({
            "file_path": self.fixture, "old_string": " + 1", "new_string": "\n", "replace_all": True,
        })
        self.assertTrue(result.is_error)
        self.assertIn("65 replacements", result.content)
        self.assertEqual(Path(self.fixture).read_text().count(" + 1"), 65)

    def test_replace_all_within_limit(self):
        Path(self.fixture).write_text("".join(f"v{i} = {i} + 1\n" for i in range(50)))
        result = self.tool.run({
            "file_path": self.fixture, "old_string": " + 1", "new_string": " + 2", "replace_all": True,
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("50 replacements", result.content)

    def test_cap_sums_across_multi_edit(self):
        Path(self.fixture).write_text("".join(f"val_{i} = {i} + 1\n" for i in range(30)))
        result = self.tool.run({
            "file_path": self.fixture, "replace_all": True,
            "edits": [
                {"old_string": " + 1", "new_string": " + 2"},
                {"old_string": "val_", "new_string": "var_"},
            ],
        })
        self.assertTrue(result.is_error)
        self.assertIn("60 replacements", result.content)
        self.assertEqual(Path(self.fixture).read_text().count("val_"), 30)

    def test_guard_stays_on_with_kill_switch(self):
        with patch.dict(os.environ, {"HARNESS_EDIT_TIERS": "exact"}):
            result = self.tool.run({
                "file_path": self.fixture, "old_string": "1", "new_string": "2", "replace_all": True,
            })
        self.assertTrue(result.is_error)
        self.assertIn("too short", result.content)


if __name__ == "__main__":
    unittest.main()
