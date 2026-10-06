"""Tests for Write and Edit tools (M2).

Tests run against real filesystem using a temp directory — no mocks.
Sentinel is NOT invoked here (that's registry's job); these test
the tool body only.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from coding_harness.tools.edit import Edit
from coding_harness.tools.write import Write


class TestWriteTool(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tool = Write()

    def tearDown(self):
        # Clean up any files we created
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_create_new_file(self):
        path = os.path.join(self.tmpdir, "new.txt")
        result = self.tool.run({"file_path": path, "content": "hello world\n"})
        self.assertFalse(result.is_error, result.content)
        self.assertIn("created", result.content)
        self.assertEqual(Path(path).read_text(), "hello world\n")

    def test_overwrite_existing_file(self):
        path = os.path.join(self.tmpdir, "existing.txt")
        Path(path).write_text("old content\n")
        result = self.tool.run({"file_path": path, "content": "new content\n"})
        self.assertFalse(result.is_error, result.content)
        self.assertIn("overwrote", result.content)
        self.assertEqual(Path(path).read_text(), "new content\n")

    def test_overwrite_note_names_previous_line_count(self):
        path = os.path.join(self.tmpdir, "existing.txt")
        Path(path).write_text("a\nb\nc\n")
        result = self.tool.run({"file_path": path, "content": "x\n"})
        self.assertFalse(result.is_error, result.content)
        self.assertIn("note: this replaced an existing 3-line file; prefer Edit", result.content)

    def test_create_has_no_overwrite_note(self):
        path = os.path.join(self.tmpdir, "fresh.txt")
        result = self.tool.run({"file_path": path, "content": "x\n"})
        self.assertNotIn("note:", result.content)

    def test_creates_parent_directories(self):
        path = os.path.join(self.tmpdir, "a", "b", "c", "deep.txt")
        result = self.tool.run({"file_path": path, "content": "deep\n"})
        self.assertFalse(result.is_error, result.content)
        self.assertTrue(Path(path).exists())

    def test_rejects_relative_path(self):
        result = self.tool.run({"file_path": "relative.txt", "content": "nope"})
        self.assertTrue(result.is_error)
        self.assertIn("absolute", result.content)

    def test_rejects_missing_content(self):
        path = os.path.join(self.tmpdir, "test.txt")
        result = self.tool.run({"file_path": path})
        self.assertTrue(result.is_error)

    def test_rejects_missing_path(self):
        result = self.tool.run({"content": "orphan"})
        self.assertTrue(result.is_error)

    def test_metadata_includes_bytes_and_lines(self):
        path = os.path.join(self.tmpdir, "meta.txt")
        content = "line1\nline2\nline3\n"
        result = self.tool.run({"file_path": path, "content": content})
        self.assertEqual(result.metadata["bytes"], len(content))
        self.assertEqual(result.metadata["lines"], 3)


class TestEditTool(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tool = Edit()
        # Create a fixture file
        self.fixture = os.path.join(self.tmpdir, "code.py")
        Path(self.fixture).write_text(
            "def hello():\n"
            "    print('hello world')\n"
            "\n"
            "def goodbye():\n"
            "    print('goodbye world')\n"
        )

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_simple_replacement(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "hello world",
            "new_string": "hi there",
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("1 replacement", result.content)
        content = Path(self.fixture).read_text()
        self.assertIn("hi there", content)
        self.assertNotIn("hello world", content)

    def test_old_string_not_found(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "this does not exist",
            "new_string": "anything",
        })
        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content)

    def test_ambiguous_match_blocked(self):
        # "world" appears twice in the fixture
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "world",
            "new_string": "universe",
        })
        self.assertTrue(result.is_error)
        self.assertIn("2 times", result.content)

    def test_replace_all(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "world",
            "new_string": "universe",
            "replace_all": True,
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("2 replacement", result.content)
        content = Path(self.fixture).read_text()
        self.assertEqual(content.count("universe"), 2)
        self.assertEqual(content.count("world"), 0)

    def test_identical_strings_rejected(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "hello",
            "new_string": "hello",
        })
        self.assertTrue(result.is_error)
        self.assertIn("identical", result.content)

    def test_file_not_found(self):
        result = self.tool.run({
            "file_path": os.path.join(self.tmpdir, "nope.py"),
            "old_string": "x",
            "new_string": "y",
        })
        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content)

    def test_relative_path_rejected(self):
        result = self.tool.run({
            "file_path": "relative.py",
            "old_string": "x",
            "new_string": "y",
        })
        self.assertTrue(result.is_error)
        self.assertIn("absolute", result.content)

    def test_multiline_replacement(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "def hello():\n    print('hello world')",
            "new_string": "def greet(name):\n    print(f'hello {name}')",
        })
        self.assertFalse(result.is_error, result.content)
        content = Path(self.fixture).read_text()
        self.assertIn("def greet(name):", content)

    def test_preview_shows_context(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "hello world",
            "new_string": "hi there",
        })
        self.assertIn("context", result.content)

    def test_not_found_hints_nearest_window(self):
        # Two-space indent instead of four: the classic small-model whitespace slip.
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "def goodbye():\n  print('goodbye world')",
            "new_string": "def farewell():\n    print('bye')",
        })
        self.assertTrue(result.is_error)
        self.assertIn("not found", result.content)
        self.assertIn("closest match at lines 4-5", result.content)
        self.assertIn("     4\tdef goodbye():", result.content)
        self.assertIn("     5\t    print('goodbye world')", result.content)
        self.assertNotIn("first 20 lines", result.content)

    def test_not_found_no_hint_below_threshold(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ",
            "new_string": "anything",
        })
        self.assertTrue(result.is_error)
        self.assertNotIn("closest match", result.content)
        self.assertIn("first 20 lines", result.content)

    def test_not_found_hint_disabled_by_env(self):
        with patch.dict(os.environ, {"HARNESS_EDIT_HINT": "0"}):
            result = self.tool.run({
                "file_path": self.fixture,
                "old_string": "def goodbye():\n  print('goodbye world')",
                "new_string": "x",
            })
        self.assertTrue(result.is_error)
        self.assertNotIn("closest match", result.content)
        self.assertIn("first 20 lines", result.content)

    def test_identical_with_occurrence_shows_region(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "def goodbye():\n    print('goodbye world')\n",
            "new_string": "def goodbye():\n    print('goodbye world')\n",
        })
        self.assertTrue(result.is_error)
        self.assertIn("identical", result.content)
        self.assertIn("already present at lines 4-5", result.content)
        self.assertIn("     4\tdef goodbye():", result.content)
        self.assertIn("do not edit it again", result.content)
        self.assertIn("goodbye world", Path(self.fixture).read_text())

    def test_identical_without_occurrence_keeps_old_text(self):
        result = self.tool.run({
            "file_path": self.fixture,
            "old_string": "nowhere",
            "new_string": "nowhere",
        })
        self.assertTrue(result.is_error)
        self.assertEqual(result.content, "error: old_string and new_string are identical")

    def test_not_found_hint_aligns_on_long_block(self):
        # Over 200 chars difflib's autojunk heuristic distorts ratios and the
        # best-scoring window drifts off the real region by a couple of lines.
        body = "".join(
            f"    result_{i} = transform(data[{i}], weight={i}) + bias_{i}\n" for i in range(12)
        )
        path = os.path.join(self.tmpdir, "long.py")
        Path(path).write_text("import x\n\ndef f(data):\n" + body + "    return result_0\n")
        # The indent tier would accept this tab-for-space slip; pin exact so the hint path runs.
        with patch.dict(os.environ, {"HARNESS_EDIT_TIERS": "exact"}):
            result = self.tool.run({
                "file_path": path,
                "old_string": body.replace("    ", "\t"),
                "new_string": "pass",
            })
        self.assertTrue(result.is_error)
        self.assertIn("closest match at lines 4-15", result.content)

    def test_not_found_hint_fast_on_large_file(self):
        big = os.path.join(self.tmpdir, "big.py")
        Path(big).write_text("".join(f"value_{i} = compute({i}) + offset\n" for i in range(6000)))
        started = time.perf_counter()
        result = self.tool.run({
            "file_path": big,
            "old_string": "value_4242 =  compute(4242) + offset",
            "new_string": "value_4242 = compute(4242)",
        })
        elapsed = time.perf_counter() - started
        self.assertTrue(result.is_error)
        self.assertIn("closest match at lines 4243-4243", result.content)
        self.assertLess(elapsed, 1.0)


if __name__ == "__main__":
    unittest.main()
