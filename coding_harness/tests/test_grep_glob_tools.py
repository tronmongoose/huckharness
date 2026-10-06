"""Tests for Grep and Glob tools.

Tests run against real filesystem using a temp directory — no mocks.
Sentinel is NOT invoked here (that's registry's job); these test
the tool body only.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from coding_harness.tools.glob_tool import Glob
from coding_harness.tools.grep import Grep


class TestGrepTool(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tool = Grep()
        # Create fixture files
        Path(self.tmpdir, "hello.py").write_text(
            "def hello():\n    print('hello world')\n\ndef goodbye():\n    print('bye')\n"
        )
        Path(self.tmpdir, "main.py").write_text(
            "from hello import hello\n\nhello()\n"
        )
        Path(self.tmpdir, "readme.md").write_text(
            "# Hello Project\n\nThis is a test project.\n"
        )
        sub = Path(self.tmpdir, "sub")
        sub.mkdir()
        Path(sub, "nested.py").write_text("# nested\nimport os\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_files_with_matches(self):
        result = self.tool.run({
            "pattern": "hello",
            "path": self.tmpdir,
            "output_mode": "files_with_matches",
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("hello.py", result.content)
        self.assertIn("main.py", result.content)
        self.assertNotIn("readme.md", result.content)

    def test_content_mode(self):
        result = self.tool.run({
            "pattern": "hello",
            "path": self.tmpdir,
            "output_mode": "content",
        })
        self.assertFalse(result.is_error, result.content)
        # Should contain line numbers in content mode
        self.assertIn("hello", result.content)

    def test_glob_filter(self):
        result = self.tool.run({
            "pattern": "hello|test",
            "path": self.tmpdir,
            "glob": "*.md",
            "output_mode": "files_with_matches",
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("readme.md", result.content)
        self.assertNotIn("hello.py", result.content)

    def test_case_insensitive(self):
        result = self.tool.run({
            "pattern": "HELLO",
            "path": self.tmpdir,
            "output_mode": "files_with_matches",
            "case_insensitive": True,
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("hello.py", result.content)

    def test_no_matches(self):
        result = self.tool.run({
            "pattern": "zzz_no_match_zzz",
            "path": self.tmpdir,
        })
        self.assertFalse(result.is_error)
        self.assertIn("no matches", result.content)

    def test_missing_pattern(self):
        result = self.tool.run({"path": self.tmpdir})
        self.assertTrue(result.is_error)

    def test_bad_path(self):
        result = self.tool.run({
            "pattern": "hello",
            "path": "/nonexistent/path/zzz",
        })
        self.assertTrue(result.is_error)

    def test_recursive_search(self):
        result = self.tool.run({
            "pattern": "nested",
            "path": self.tmpdir,
            "output_mode": "files_with_matches",
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("nested.py", result.content)


class TestGlobTool(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.tool = Glob()
        # Create fixture files
        Path(self.tmpdir, "a.py").write_text("# a\n")
        Path(self.tmpdir, "b.py").write_text("# b\n")
        Path(self.tmpdir, "readme.md").write_text("# readme\n")
        sub = Path(self.tmpdir, "src")
        sub.mkdir()
        Path(sub, "c.py").write_text("# c\n")
        Path(sub, "d.ts").write_text("// d\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_find_python_files(self):
        result = self.tool.run({
            "pattern": "**/*.py",
            "path": self.tmpdir,
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("a.py", result.content)
        self.assertIn("b.py", result.content)
        self.assertIn("c.py", result.content)
        self.assertNotIn("readme.md", result.content)
        self.assertNotIn("d.ts", result.content)

    def test_find_markdown(self):
        result = self.tool.run({
            "pattern": "*.md",
            "path": self.tmpdir,
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("readme.md", result.content)

    def test_find_typescript_in_subdir(self):
        result = self.tool.run({
            "pattern": "**/*.ts",
            "path": self.tmpdir,
        })
        self.assertFalse(result.is_error, result.content)
        self.assertIn("d.ts", result.content)

    def test_no_matches(self):
        result = self.tool.run({
            "pattern": "**/*.rs",
            "path": self.tmpdir,
        })
        self.assertFalse(result.is_error)
        self.assertIn("no files", result.content)

    def test_missing_pattern(self):
        result = self.tool.run({"path": self.tmpdir})
        self.assertTrue(result.is_error)

    def test_bad_path(self):
        result = self.tool.run({
            "pattern": "*.py",
            "path": "/nonexistent/zzz",
        })
        self.assertTrue(result.is_error)

    def test_not_a_directory(self):
        filepath = os.path.join(self.tmpdir, "a.py")
        result = self.tool.run({
            "pattern": "*.py",
            "path": filepath,
        })
        self.assertTrue(result.is_error)
        self.assertIn("not a directory", result.content)

    def test_metadata(self):
        result = self.tool.run({
            "pattern": "**/*.py",
            "path": self.tmpdir,
        })
        self.assertIn("total", result.metadata)
        self.assertEqual(result.metadata["total"], 3)


if __name__ == "__main__":
    unittest.main()
