"""Green-before-done gate units (P1-4): test command discovery on fixture
trees, targeted test lookup, the ported allowlist with its shared timeout and
failure extraction, and baseline/compare. The session loop is covered in
test_done_gate_session.py."""
from __future__ import annotations

import json
import shlex
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core import done_gate
from coding_harness.core.done_gate import (
    PYTEST,
    CheckReport,
    baseline,
    compare,
    discover_test_cmd,
    pytest_cmd,
    resolve_cmds,
    run_checks,
    targeted_tests,
)


class _Tree(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, body: str = "") -> str:
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        return str(p)


class DiscoverTests(_Tree):
    def test_tests_dir_or_pyproject_means_pytest(self) -> None:
        self.assertIsNone(discover_test_cmd(str(self.root)))
        (self.root / "tests").mkdir()
        self.assertEqual(discover_test_cmd(str(self.root)), PYTEST)
        self.assertEqual(PYTEST, [sys.executable, "-m", "pytest", "-q"])
        other = self.root / "other"
        other.mkdir()
        (other / "pyproject.toml").write_text('[project.optional-dependencies]\ndev = ["pytest"]\n')
        self.assertEqual(discover_test_cmd(str(other)), PYTEST)

    def test_makefile_test_target(self) -> None:
        self._write("Makefile", "lint:\n\t@true\n")
        self.assertIsNone(discover_test_cmd(str(self.root)))
        self._write("Makefile", "lint:\n\t@true\ntest: ## run\n\t@true\n")
        self.assertEqual(discover_test_cmd(str(self.root)), ["make", "test"])

    def test_package_json_scripts_test(self) -> None:
        self._write("package.json", json.dumps({"scripts": {"build": "x"}}))
        self.assertIsNone(discover_test_cmd(str(self.root)))
        self._write("package.json", json.dumps({"scripts": {"test": "jest"}}))
        self.assertEqual(discover_test_cmd(str(self.root)), ["npm", "test"])
        self._write("package.json", "{not json")
        self.assertIsNone(discover_test_cmd(str(self.root)))

    def test_go_and_cargo(self) -> None:
        self._write("Cargo.toml", "[package]\n")
        self.assertEqual(discover_test_cmd(str(self.root)), ["cargo", "test"])
        self._write("go.mod", "module m\n")
        self.assertEqual(discover_test_cmd(str(self.root)), ["go", "test", "./..."])

    def test_make_target_wins_over_pytest(self) -> None:
        # The project's declared gate knows what to skip; bare pytest would
        # collect fixtures that are meant to fail.
        (self.root / "tests").mkdir()
        self._write("Makefile", "test:\n\t@true\n")
        self.assertEqual(discover_test_cmd(str(self.root)), ["make", "test"])

    def test_pytest_runs_on_the_target_repo_interpreter(self) -> None:
        (self.root / "tests").mkdir()
        probed = {"tools": {"python": {"path": "/fake/.venv/bin/python", "version": "3.12.1"}}}
        with mock.patch.object(done_gate.toolprobe, "probe", return_value=probed) as probe:
            self.assertEqual(pytest_cmd(str(self.root)), ["/fake/.venv/bin/python", "-m", "pytest", "-q"])
            self.assertEqual(discover_test_cmd(str(self.root)), ["/fake/.venv/bin/python", "-m", "pytest", "-q"])
        probe.assert_called_with(str(self.root))
        for absent in ({}, {"tools": {}, "missing": ["python"]}):
            with mock.patch.object(done_gate.toolprobe, "probe", return_value=absent):
                self.assertEqual(pytest_cmd(str(self.root)), PYTEST)


class TargetedTests(_Tree):
    def setUp(self) -> None:
        super().setUp()
        self.mod = self._write("pkg/mod.py", "x = 1\n")
        self.thing = self._write("pkg/thing.py", "y = 1\n")
        self.t_mod = self._write("tests/test_mod.py", "from mod import x\n")
        self.t_thing = self._write("tests/test_helpers.py", "import thing\nimport mod\n")
        self._write("tests/test_other.py", "pass\n")
        self._write(".venv/test_hidden.py", "import thing\nimport secret\n")

    def test_exact_name_wins_over_grep(self) -> None:
        self.assertEqual(targeted_tests([self.mod], str(self.root)), ([self.t_mod], None))

    def test_grep_for_stem_skips_hidden_trees(self) -> None:
        self.assertEqual(targeted_tests([self.thing], str(self.root)), ([self.t_thing], None))
        self.assertEqual(targeted_tests([self._write("pkg/secret.py")], str(self.root)), ([], None))

    def test_edited_test_file_is_its_own_target(self) -> None:
        t = self._write("tests/test_new.py", "pass\n")
        self.assertEqual(targeted_tests([t, self._write("README.md")], str(self.root)), ([t], None))

    def test_capped_at_five_with_a_note(self) -> None:
        wide = self._write("pkg/wide.py")
        for i in range(7):
            self._write(f"tests/test_w{i}.py", "import wide\n")
        files, note = targeted_tests([wide], str(self.root))
        self.assertEqual(len(files), done_gate.MAX_TARGETED_FILES)
        self.assertEqual(note, "7 test files match this edit; ran the first 5 (importers first)")

    def test_cut_keeps_the_importers(self) -> None:
        wide = self._write("pkg/wide.py")
        for i in range(6):
            self._write(f"tests/test_a{i}.py", "# mentions wide in a comment\n")
        by_from = self._write("tests/test_y.py", "from pkg.wide import thing\n")
        by_import = self._write("tests/test_z.py", "import os, wide\n")
        files, note = targeted_tests([wide], str(self.root))
        self.assertEqual(files[:2], [by_from, by_import])
        self.assertEqual(len(files), done_gate.MAX_TARGETED_FILES)
        self.assertTrue(note.startswith("8 test files match"))


class RunChecksTests(_Tree):
    def test_allowlist(self) -> None:
        for ok in ("pytest -q", "python -m pytest tests", f"{sys.executable} -m pytest",
                   "make test", "npm test", "go test ./...", "cargo test", "ruff check .",
                   "bash run.sh"):
            self.assertTrue(done_gate._allowed(ok), ok)
        for bad in ("python -c 'print(1)'", "python3 -m py_compile x.py", "bash -c 'rm x'",
                    "grep -q x y", "rm -rf /", "", "bash", "'unterminated"):
            self.assertFalse(done_gate._allowed(bad), bad)

    def test_not_allowlisted_is_skip(self) -> None:
        report = run_checks(["grep -q x y"], str(self.root), 5)
        self.assertEqual((report.passed, report.skipped_reason), (None, "no runnable checks"))
        self.assertIn("SKIP (not in allowlist): grep -q x y", report.report)
        self.assertEqual(run_checks([], str(self.root), 5).skipped_reason, "no checks")

    def test_pytest_failure_ids_extracted(self) -> None:
        self._write("tests/test_t.py", "def test_ok():\n    assert 1\n\ndef test_bad():\n    assert 0, 'nope'\n")
        cmd = shlex.join(PYTEST + ["tests/test_t.py"])
        report = run_checks([cmd], str(self.root), 60)
        self.assertIs(report.passed, False)
        self.assertEqual(len(report.failures), 1)
        self.assertTrue(report.failures[0].endswith("test_t.py::test_bad"), report.failures)
        self.assertIn(f"FAIL: {cmd}", report.report)
        self.assertIn("AssertionError", report.report)

    def _two_failing_tests(self) -> str:
        return self._write(
            "tests/test_two.py",
            "def test_a():\n    assert 0, 'old a'\n\ndef test_b():\n    assert 0, 'old b'\n\n"
            "def test_c():\n    assert 1\n",
        )

    def test_baseline_holds_every_failure_not_just_the_first(self) -> None:
        self._two_failing_tests()
        cmd = shlex.join(PYTEST + ["tests/test_two.py"])
        before = baseline([cmd], str(self.root), 60)
        self.assertEqual({f.rsplit("::", 1)[1] for f in before}, {"test_a", "test_b"})
        # Fixing test_a leaves only the pre-existing test_b: undecidable, not a new failure.
        self._write("tests/test_two.py",
                    "def test_a():\n    assert 1\n\ndef test_b():\n    assert 0, 'old b'\n")
        out = compare(before, run_checks([cmd], str(self.root), 60))
        self.assertEqual((out.passed, out.skipped_reason), (None, "baseline_red:2 failures"))
        # A new failure next to the pre-existing one is caught even when it sorts after it.
        self._write("tests/test_two.py",
                    "def test_a():\n    assert 1\n\ndef test_b():\n    assert 0, 'old b'\n\n"
                    "def test_c():\n    assert 0, 'new'\n")
        out = compare(before, run_checks([cmd], str(self.root), 60))
        self.assertIs(out.passed, False)
        self.assertEqual([f.rsplit("::", 1)[1] for f in out.failures], ["test_c"])

    def test_plain_failure_uses_command_as_id(self) -> None:
        self._write("ok.sh", "exit 0\n")
        self._write("fail.sh", "echo boom >&2\nexit 1\n")
        report = run_checks(["bash ok.sh", "bash fail.sh"], str(self.root), 10)
        self.assertEqual((report.passed, report.failures), (False, ["bash fail.sh"]))
        self.assertIn("PASS: bash ok.sh", report.report)
        self.assertIn("       boom", report.report)
        self.assertIs(run_checks(["bash ok.sh"], str(self.root), 10).passed, True)

    def test_budget_is_shared_across_commands(self) -> None:
        self._write("slow.sh", "sleep 5\n")
        self._write("ok.sh", "exit 0\n")
        report = run_checks(["bash slow.sh", "bash ok.sh"], str(self.root), 1.0)
        self.assertEqual(report.failures, ["TIMEOUT: bash slow.sh", "TIMEOUT: bash ok.sh"])
        self.assertIs(report.passed, False)


class CompareTests(_Tree):
    def test_no_new_failures_passes(self) -> None:
        self.assertIs(compare({"a"}, CheckReport(True, [], "ok")).passed, True)

    def test_new_failure_fails_with_only_the_new_ids(self) -> None:
        out = compare({"a"}, CheckReport(False, ["a", "b"], "r"))
        self.assertEqual((out.passed, out.failures, out.report), (False, ["b"], "r"))
        out = compare(set(), CheckReport(False, ["a"], "r"))
        self.assertEqual((out.passed, out.failures), (False, ["a"]))

    def test_red_baseline_is_undecidable(self) -> None:
        out = compare({"a", "b"}, CheckReport(False, ["a"], "r"))
        self.assertEqual((out.passed, out.skipped_reason), (None, "baseline_red:2 failures"))
        self.assertEqual(out.failures, ["a"])

    def test_nothing_ran_passes_through(self) -> None:
        rep = CheckReport(None, [], "", "no checks")
        self.assertIs(compare({"a"}, rep), rep)

    def test_baseline_and_resolve(self) -> None:
        self._write("fail.sh", "exit 1\n")
        self.assertEqual(baseline(["bash fail.sh"], str(self.root), 5), {"bash fail.sh"})
        self.assertEqual(resolve_cmds(["make x"], ["make", "test"], str(self.root)), ["make x"])
        self.assertEqual(resolve_cmds([], ["make", "test"], str(self.root)), ["make test"])
        self.assertEqual(resolve_cmds([], None, str(self.root)), [])
        (self.root / "tests").mkdir()
        self.assertEqual(resolve_cmds([], None, str(self.root)), [shlex.join(PYTEST)])

    def test_budget_share_of_remaining(self) -> None:
        self.assertEqual(done_gate._budget(120, 0.2, None), 120)
        self.assertEqual(done_gate._budget(120, 0.2, 100), 20)
        self.assertEqual(done_gate._budget(300, 0.4, 10000), 300)


if __name__ == "__main__":
    unittest.main()
