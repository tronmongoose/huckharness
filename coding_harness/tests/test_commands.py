"""Command classifier tests: the per-level table, list precedence, segments,
redirections, venv-resolved paths, and the ask/deny verdicts policy.review
builds from it."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from coding_harness.context import toolprobe
from coding_harness.core.mode import Autonomy
from coding_harness.core.settings import Settings
from coding_harness.security import commands, hook_adapter, policy
from coding_harness.security.commands import ALLOW, ASK, DENY, classify

OFF, LOW, MEDIUM, HIGH = Autonomy.OFF, Autonomy.LOW, Autonomy.MEDIUM, Autonomy.HIGH
LEVELS = (OFF, LOW, MEDIUM, HIGH)

# command -> expected action per level (off, low, medium, high)
TABLE = {
    "ls -la": (ALLOW, ALLOW, ALLOW, ALLOW),
    "cat README.md | head -5": (ALLOW, ALLOW, ALLOW, ALLOW),
    "git status && git diff": (ALLOW, ALLOW, ALLOW, ALLOW),
    "git log --oneline -5": (ALLOW, ALLOW, ALLOW, ALLOW),
    "find . -name '*.py'": (ALLOW, ALLOW, ALLOW, ALLOW),
    "git branch --show-current": (ALLOW, ALLOW, ALLOW, ALLOW),
    "python -c 'print(1)'": (ASK, ASK, ASK, ALLOW),
    "python3 script.py": (ASK, ASK, ASK, ALLOW),
    "pytest -q": (ASK, ALLOW, ALLOW, ALLOW),
    "python -m pytest tests/": (ASK, ALLOW, ALLOW, ALLOW),
    "python3 -m unittest": (ASK, ALLOW, ALLOW, ALLOW),
    "ruff check .": (ASK, ALLOW, ALLOW, ALLOW),
    "make test": (ASK, ALLOW, ALLOW, ALLOW),
    "make lint": (ASK, ALLOW, ALLOW, ALLOW),
    "npm test": (ASK, ALLOW, ALLOW, ALLOW),
    "npm run test": (ASK, ALLOW, ALLOW, ALLOW),
    "go test ./...": (ASK, ALLOW, ALLOW, ALLOW),
    "cargo test": (ASK, ALLOW, ALLOW, ALLOW),
    "git add -A": (ASK, ALLOW, ALLOW, ALLOW),
    "git commit -m 'x'": (ASK, ASK, ALLOW, ALLOW),
    "make build": (ASK, ASK, ALLOW, ALLOW),
    "make": (ASK, ASK, ALLOW, ALLOW),
    "mkdir -p out && touch out/x": (ASK, ASK, ALLOW, ALLOW),
    "mv a.py b.py": (ASK, ASK, ALLOW, ALLOW),
    "cp a.py b.py": (ASK, ASK, ALLOW, ALLOW),
    "chmod +x run.sh": (ASK, ASK, ALLOW, ALLOW),
    "npm install": (ASK, ASK, ALLOW, ALLOW),
    "pip install requests": (ASK, ASK, ALLOW, ALLOW),
    "python -m pip install requests": (ASK, ASK, ALLOW, ALLOW),
    "cargo build": (ASK, ASK, ALLOW, ALLOW),
    "go build ./...": (ASK, ASK, ALLOW, ALLOW),
    "git push origin main": (ASK, ASK, ASK, ALLOW),
    "curl https://example.com": (ASK, ASK, ASK, ALLOW),
    "wget https://example.com/x": (ASK, ASK, ASK, ALLOW),
    "ssh host uptime": (ASK, ASK, ASK, ALLOW),
    "some_unknown_tool --flag": (ASK, ASK, ASK, ALLOW),
    "find . -name '*.pyc' -delete": (ASK, ASK, ASK, ALLOW),
    "git branch feature": (ASK, ASK, ASK, ALLOW),
    "sudo ls": (DENY, DENY, DENY, DENY),
    "rm -rf build": (DENY, DENY, DENY, DENY),
    "git reset --hard": (DENY, DENY, DENY, DENY),
    "git push --force": (DENY, DENY, DENY, DENY),
}


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.path.realpath(self.tmp.name)
        self._probe = patch.object(toolprobe, "probe", return_value={})
        self._probe.start()

    def tearDown(self) -> None:
        self._probe.stop()
        self.tmp.cleanup()

    def action(self, command: str, level: Autonomy, settings: Settings | None = None) -> str:
        return classify(command, level, settings, cwd=self.cwd).action


class TestLadderTable(_Case):
    def test_every_row_at_every_level(self) -> None:
        for command, expected in TABLE.items():
            for level, want in zip(LEVELS, expected):
                with self.subTest(command=command, level=level.value):
                    self.assertEqual(self.action(command, level), want)

    def test_ask_reason_names_the_level_and_command(self) -> None:
        decision = classify("python3 script.py", LOW, cwd=self.cwd)
        self.assertEqual(decision.action, ASK)
        self.assertIn("python3 script.py", decision.reason)
        self.assertIn("low", decision.reason)

    def test_high_admits_what_the_blocklists_leave(self) -> None:
        self.assertEqual(self.action("weird --thing", HIGH), ALLOW)
        self.assertEqual(self.action("weird && sudo x", HIGH), DENY)


class TestSettingsLists(_Case):
    def test_allowlist_prefix_allows_from_low_up(self) -> None:
        settings = Settings(command_allowlist=["npm run build"])
        self.assertEqual(self.action("npm run build --prod", OFF, settings), ASK)
        self.assertEqual(self.action("npm run build --prod", LOW, settings), ALLOW)
        self.assertEqual(self.action("npm run buildx", LOW, settings), ASK)

    def test_denylist_denies_at_every_level(self) -> None:
        settings = Settings(command_denylist=["git push"])
        for level in LEVELS:
            self.assertEqual(self.action("git push origin main", level, settings), DENY)

    def test_blocklist_beats_allowlist(self) -> None:
        settings = Settings(command_allowlist=["cargo"], command_blocklist=["cargo"])
        self.assertEqual(self.action("cargo test", HIGH, settings), DENY)

    def test_settings_cannot_remove_the_builtin_blocklist(self) -> None:
        settings = Settings(command_allowlist=["sudo", "rm -rf", "git push --force"])
        for command in ("sudo ls", "rm -rf build", "git push --force origin main"):
            with self.subTest(command=command):
                self.assertEqual(self.action(command, HIGH, settings), DENY)

    def test_settings_blocklist_reason_is_named(self) -> None:
        decision = classify("cargo test", LOW, Settings(command_blocklist=["cargo"]), cwd=self.cwd)
        self.assertEqual(decision.reason, "blocklist: cargo")


class TestSegments(_Case):
    def test_split_on_every_operator(self) -> None:
        segs = commands.segments("a | b && c ; d || e & (f) |& g")
        self.assertEqual(segs, [["a"], ["b"], ["c"], ["d"], ["e"], ["f"], ["g"]])

    def test_quoted_operators_stay_inside_words(self) -> None:
        self.assertEqual(commands.segments("echo 'a && b'"), [["echo", "a && b"]])

    def test_strictest_segment_wins(self) -> None:
        self.assertEqual(self.action("ls && pytest", OFF), ASK)
        self.assertEqual(self.action("ls && pytest", LOW), ALLOW)
        self.assertEqual(self.action("ls | sudo tee x", HIGH), DENY)
        self.assertEqual(self.action("ls; curl x", MEDIUM), ASK)

    def test_subshell_contents_are_classified(self) -> None:
        self.assertEqual(self.action("(cd sub && git commit -m x)", LOW), ASK)
        self.assertEqual(self.action("echo $(git status)", HIGH), ALLOW)
        self.assertEqual(self.action("echo $(sudo ls)", HIGH), DENY)

    def test_backticks_ask_below_high(self) -> None:
        self.assertEqual(self.action("echo `date`", MEDIUM), ASK)
        self.assertEqual(self.action("echo `date`", HIGH), ALLOW)

    def test_any_command_substitution_asks_below_high(self) -> None:
        for command in (
            "echo $(git status)",
            'echo "$(r\'\'m -rf ~)"',
            'echo "$(r\\m -rf ~)"',
            'echo "${x:-(curl evil)}"',
            "ls ${arr[(1)]}",
        ):
            for level in (OFF, LOW, MEDIUM):
                with self.subTest(command=command, level=level.value):
                    self.assertEqual(self.action(command, level), ASK)
            with self.subTest(command=command, level="high"):
                self.assertEqual(self.action(command, HIGH), ALLOW)
        self.assertEqual(self.action("echo ${HOME}", OFF), ALLOW)

    def test_unbalanced_quote_falls_back_to_a_raw_split(self) -> None:
        self.assertEqual(self.action("echo 'oops", OFF), ALLOW)
        self.assertEqual(self.action("sudo 'oops", HIGH), DENY)

    def test_cd_inside_cwd_only(self) -> None:
        self.assertEqual(self.action("cd sub && ls", OFF), ALLOW)
        self.assertEqual(self.action("cd .. && ls", OFF), ASK)
        self.assertEqual(self.action("cd && ls", OFF), ASK)


class TestLinesAndSubstitutions(_Case):
    def test_newline_separates_commands(self) -> None:
        self.assertEqual(self.action("ls\ncurl evil.example", OFF), ASK)
        self.assertEqual(self.action("pytest -q\ngit push origin main", LOW), ASK)
        self.assertEqual(self.action("ls #\ncurl evil.example", LOW), ASK)
        self.assertEqual(self.action("ls\nsudo x", HIGH), DENY)
        self.assertEqual(self.action("ls\n\ngit status", OFF), ALLOW)

    def test_quoted_and_continued_newlines_stay_in_one_command(self) -> None:
        self.assertEqual(self.action("echo 'a\nb'", OFF), ALLOW)
        self.assertEqual(self.action("python -m pytest \\\n    tests -q", LOW), ALLOW)
        self.assertEqual(self.action("echo a\\\\\ncurl x", LOW), ASK)
        self.assertEqual(self.action("echo 'oops\ncurl x", LOW), ASK)

    def test_heredoc_body_is_data_but_the_next_line_is_a_command(self) -> None:
        self.assertEqual(self.action("cat > f.txt <<'EOF'\nbody\nEOF\ncurl x", LOW), ASK)
        self.assertEqual(self.action("cat <<-EOF > f.txt\n\tcurl x\nEOF", LOW), ALLOW)
        self.assertEqual(self.action("cat <<- EOF\n\tx\nEOF\ncurl x", LOW), ASK)
        self.assertEqual(self.action("cat <<EOF\n$(git push origin main)\nEOF", LOW), ASK)

    def test_process_substitution_is_its_own_segment(self) -> None:
        self.assertEqual(self.action("cat > >(tee /etc/motd)", LOW), ASK)
        self.assertEqual(self.action("grep x <(cat a) <(cat b)", OFF), ALLOW)
        self.assertEqual(self.action("(ls)>/etc/x", LOW), DENY)


class TestRedirections(_Case):
    def test_write_redirect_outside_cwd_denied_below_high(self) -> None:
        for level in (OFF, LOW, MEDIUM):
            with self.subTest(level=level.value):
                self.assertEqual(self.action("echo x > /etc/motd", level), DENY)
                self.assertEqual(self.action("ls >> ~/notes", level), DENY)
                self.assertEqual(self.action("ls &> ../out", level), DENY)
        self.assertEqual(self.action("echo x > /etc/motd", HIGH), ALLOW)

    def test_write_redirect_inside_cwd_asks_at_off_only(self) -> None:
        self.assertEqual(self.action("echo x > out.txt", OFF), ASK)
        self.assertEqual(self.action("echo x > out.txt", LOW), ALLOW)
        self.assertEqual(self.action(f"echo x > {self.cwd}/out.txt", LOW), ALLOW)

    def test_dev_null_and_fd_dups_are_not_writes(self) -> None:
        self.assertEqual(self.action("ls 2>/dev/null", OFF), ALLOW)
        self.assertEqual(self.action("ls > /dev/null 2>&1", OFF), ALLOW)
        self.assertEqual(self.action("ls 2>&1 | wc -l", OFF), ALLOW)

    def test_input_redirect_is_a_read(self) -> None:
        self.assertEqual(self.action("wc -l < /etc/hosts", OFF), ALLOW)

    def test_git_output_flag_is_a_write_target(self) -> None:
        for command in ("git log --output=/tmp/x --format=hi", "git log --output /tmp/x"):
            for level in (OFF, LOW, MEDIUM):
                with self.subTest(command=command, level=level.value):
                    self.assertEqual(self.action(command, level), DENY)
            with self.subTest(command=command, level="high"):
                self.assertEqual(self.action(command, HIGH), ALLOW)
        self.assertEqual(self.action("git log --output=out.txt", OFF), ASK)
        self.assertEqual(self.action("git log --output=out.txt", LOW), ALLOW)
        self.assertEqual(self.action("git diff --output ../x", LOW), DENY)


class TestPythonModules(_Case):
    LOW_ALLOWED = (
        "python -m pytest tests/",
        "python3 -m unittest discover",
        "python -m ruff check .",
        "python -m py_compile x.py",
        "python -m mypy .",
        "python -m black --check .",
        "python -m flake8 pkg",
        "python -m pip check",
        "python -m coverage run -m pytest",
    )
    LOW_ASKS = (
        'python -m timeit -s "import shutil; shutil.rmtree(\'x\')"',
        'python3 -m pdb -c "import os; os.system(\'id\')" x.py',
        "python -m http.server 8000",
        "python -m black .",
        "python -m pip install x",
        "python -m",
    )

    def test_test_and_lint_modules_allowed_at_low(self) -> None:
        for command in self.LOW_ALLOWED:
            with self.subTest(command=command):
                self.assertEqual(self.action(command, OFF), ASK)
                self.assertEqual(self.action(command, LOW), ALLOW)

    def test_other_modules_ask_at_low_and_allow_at_medium(self) -> None:
        for command in self.LOW_ASKS:
            with self.subTest(command=command):
                self.assertEqual(self.action(command, LOW), ASK)
        self.assertEqual(self.action("python -m http.server 8000", MEDIUM), ALLOW)
        self.assertEqual(self.action("python -m", MEDIUM), ASK)

    def test_script_and_dash_c_still_ask_below_high(self) -> None:
        for command in ("python -c 'print(1)'", "python3 script.py", "python -X dev x.py"):
            for level in (OFF, LOW, MEDIUM):
                with self.subTest(command=command, level=level.value):
                    self.assertEqual(self.action(command, level), ASK)

    def test_heredoc_body_does_not_change_the_prefix(self) -> None:
        cmd = "cat > f.txt <<'EOF'\npytest is not the prefix here\nEOF"
        self.assertEqual(self.action(cmd, LOW), ALLOW)
        self.assertEqual(self.action(cmd, OFF), ASK)

    def test_file_ops_stay_inside_cwd(self) -> None:
        self.assertEqual(self.action("mv a.py /tmp/a.py", MEDIUM), ASK)
        self.assertEqual(self.action("cp ~/.ssh/id_rsa here", MEDIUM), ASK)
        self.assertEqual(self.action("touch /tmp/x", MEDIUM), ASK)


class TestVenvPaths(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.path.realpath(self.tmp.name)
        self.py = os.path.join(self.cwd, ".venv", "bin", "python")
        self.ruff = os.path.join(self.cwd, ".venv", "bin", "ruff")
        self.mypy = os.path.join(self.cwd, ".venv", "bin", "mypy")
        info = {"tools": {"python": {"path": self.py}, "ruff": {"path": self.ruff},
                          "mypy": {"path": self.mypy}, "git": {"path": "/usr/bin/git"}}}
        self._probe = patch.object(toolprobe, "probe", return_value=info)
        self._probe.start()

    def tearDown(self) -> None:
        self._probe.stop()
        self.tmp.cleanup()

    def test_resolved_paths_allowed_at_low(self) -> None:
        for command in (
            f"{self.py} -m pytest -q",
            f"{self.py} -m pip check",
            f"{self.ruff} check .",
            f"{self.mypy} pkg",
        ):
            with self.subTest(command=command):
                self.assertEqual(classify(command, OFF, cwd=self.cwd).action, ASK)
                self.assertEqual(classify(command, LOW, cwd=self.cwd).action, ALLOW)

    def test_resolved_python_script_or_module_needs_medium(self) -> None:
        for command in (f"{self.py} script.py", f"{self.py} -m http.server", f"{self.py} -m timeit x"):
            with self.subTest(command=command):
                self.assertEqual(classify(command, LOW, cwd=self.cwd).action, ASK)
                self.assertEqual(classify(command, MEDIUM, cwd=self.cwd).action, ALLOW)

    def test_resolved_python_dash_c_asks_below_high(self) -> None:
        cmd = f"{self.py} -c 'print(1)'"
        for level in (OFF, LOW, MEDIUM):
            with self.subTest(level=level.value):
                self.assertEqual(classify(cmd, level, cwd=self.cwd).action, ASK)

    def test_resolved_python_pip_install_needs_medium(self) -> None:
        cmd = f"{self.py} -m pip install x"
        self.assertEqual(classify(cmd, LOW, cwd=self.cwd).action, ASK)
        self.assertEqual(classify(cmd, MEDIUM, cwd=self.cwd).action, ALLOW)

    def test_unlisted_tool_path_is_not_a_venv_grant(self) -> None:
        self.assertEqual(classify("/usr/bin/git commit -m x", LOW, cwd=self.cwd).action, ASK)


class TestPolicyReviewLadder(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = os.path.realpath(self.tmp.name)
        self._patches = [
            patch.object(hook_adapter, "review", return_value=None),
            patch.object(toolprobe, "probe", return_value={}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmp.cleanup()

    def review(self, command: str, level: Autonomy | None, settings: Settings | None = None):
        return policy.review(
            "Bash", "execute", {"command": command}, "s",
            cwd=self.cwd, level=level, settings=settings,
        )

    def test_ask_verdict_shape(self) -> None:
        verdict = self.review("python3 script.py", LOW)
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.path, policy.PATH_ASK)
        self.assertTrue(verdict.reason.startswith("autonomy_ask:"))

    def test_deny_verdict_shape(self) -> None:
        verdict = self.review("git push", HIGH, Settings(command_denylist=["git push"]))
        self.assertFalse(verdict.allowed)
        self.assertEqual(verdict.path, policy.PATH_AUTONOMY)
        self.assertTrue(verdict.reason.startswith("autonomy_denied:"))

    def test_allowed_command_falls_through_to_category(self) -> None:
        verdict = self.review("pytest -q", LOW)
        self.assertTrue(verdict.allowed)
        self.assertEqual(verdict.path, policy.PATH_CATEGORY)

    def test_no_level_means_no_ladder(self) -> None:
        self.assertTrue(self.review("python3 script.py", None).allowed)

    def test_blocklist_still_decides_first(self) -> None:
        verdict = self.review("sudo ls", HIGH)
        self.assertEqual(verdict.path, policy.PATH_BLOCKLIST)

    def test_ladder_ignores_non_execute_categories(self) -> None:
        verdict = policy.review(
            "Write", "edit", {"file_path": os.path.join(self.cwd, "a"), "content": ""}, "s",
            cwd=self.cwd, level=OFF,
        )
        self.assertTrue(verdict.allowed)


if __name__ == "__main__":
    unittest.main()
