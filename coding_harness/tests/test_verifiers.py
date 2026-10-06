"""Per-suffix verifiers (P1-4): each suffix passes on a good file, fails on a
bad one, and SKIPs with a reason when its tool is absent. Tools that are not
guaranteed on a dev box (tsc, go, cargo) run against a faked subprocess so the
argv, cwd and per-root dedup are still checked."""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coding_harness.core import verifiers
from coding_harness.core.verifiers import Check, verify_paths

WHICH = "coding_harness.core.verifiers.shutil.which"
RUN = "coding_harness.core.verifiers.subprocess.run"


def _proc(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr=err)


class VerifiersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write(self, name: str, body: str) -> str:
        p = self.dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        return str(p)

    def _one(self, path: str) -> Check:
        checks = verify_paths([path], str(self.dir))
        self.assertEqual(len(checks), 1, checks)
        return checks[0]

    def _skips_without(self, path: str, binary: str) -> None:
        with mock.patch(WHICH, return_value=None):
            check = self._one(path)
        self.assertEqual((check.status, check.detail), ("SKIP", f"{binary} not on PATH"))

    # ── in-process and interpreter-backed suffixes ───────────────

    def test_py(self) -> None:
        good = self._one(self._write("ok.py", "x = 1\n"))
        self.assertEqual((good.status, good.name), ("PASS", f"py_compile {self.dir / 'ok.py'}"))
        bad = self._one(self._write("bad.py", "def broken(:\n"))
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("SyntaxError", bad.detail)

    def test_json(self) -> None:
        self.assertEqual(self._one(self._write("ok.json", '{"a": 1}\n')).status, "PASS")
        bad = self._one(self._write("bad.json", '{"a": \n'))
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("JSONDecodeError", bad.detail)

    @unittest.skipIf(sys.version_info < (3, 11), "tomllib")
    def test_toml(self) -> None:
        self.assertEqual(self._one(self._write("ok.toml", 'a = 1\n')).status, "PASS")
        bad = self._one(self._write("bad.toml", 'a = \n'))
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("TOMLDecodeError", bad.detail)

    def test_toml_skips_before_311(self) -> None:
        path = self._write("x.toml", "a = 1\n")
        with mock.patch.object(sys, "version_info", (3, 9, 0)):
            check = self._one(path)
        self.assertEqual((check.status, check.detail), ("SKIP", "needs python 3.11"))

    def test_yaml(self) -> None:
        self.assertEqual(self._one(self._write("ok.yaml", "a: 1\n")).status, "PASS")
        bad = self._one(self._write("bad.yml", "a: [1,\n"))
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("Error", bad.detail)
        path = self._write("x.yaml", "a: 1\n")
        with mock.patch.dict(sys.modules, {"yaml": None}):
            check = self._one(path)
        self.assertEqual((check.status, check.detail), ("SKIP", "pyyaml not importable"))

    # ── PATH-dependent tools that a dev box has ─────────────────

    @unittest.skipUnless(shutil.which("bash"), "bash")
    def test_sh(self) -> None:
        self.assertEqual(self._one(self._write("ok.sh", "echo hi\n")).status, "PASS")
        bad = self._one(self._write("bad.sh", "if [ 1 ]; then\n"))
        self.assertEqual((bad.status, bad.name), ("FAIL", f"bash -n {self.dir / 'bad.sh'}"))
        self.assertIn("syntax error", bad.detail)

    def test_sh_skips_without_bash(self) -> None:
        self._skips_without(self._write("x.sh", "echo\n"), "bash")

    @unittest.skipUnless(shutil.which("node"), "node")
    def test_js(self) -> None:
        self.assertEqual(self._one(self._write("ok.mjs", "const a = 1;\n")).status, "PASS")
        bad = self._one(self._write("bad.js", "const a = ;\n"))
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("SyntaxError", bad.detail)

    def test_js_skips_without_node(self) -> None:
        self._skips_without(self._write("x.js", "1\n"), "node")

    @unittest.skipUnless(shutil.which("make"), "make")
    def test_makefile(self) -> None:
        self.assertEqual(self._one(self._write("Makefile", "all:\n\t@echo hi\n")).status, "PASS")
        bad = self._one(self._write("sub/Makefile", "all:\necho hi\n"))
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("missing separator", bad.detail)

    def test_makefile_skips_without_make(self) -> None:
        self._skips_without(self._write("Makefile", "all:\n"), "make")

    # ── project-level tools, faked ──────────────────────────────

    def test_ts_needs_tsconfig_then_tsc(self) -> None:
        path = self._write("src/a.ts", "const a: number = 1;\n")
        check = self._one(path)
        self.assertEqual((check.status, check.detail), ("SKIP", "no tsconfig.json above an edited file"))
        self._write("tsconfig.json", "{}\n")
        self._skips_without(path, "tsc")
        with mock.patch(WHICH, return_value="/x/tsc"), mock.patch(RUN, return_value=_proc(0)) as run:
            good = self._one(path)
        self.assertEqual((good.status, good.name), ("PASS", f"tsc --noEmit in {self.dir}"))
        self.assertEqual(run.call_args.args[0], ["tsc", "--noEmit", "-p", str(self.dir / "tsconfig.json")])
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.dir))
        with mock.patch(WHICH, return_value="/x/tsc"), mock.patch(RUN, return_value=_proc(2, "a.ts(1,7): error TS1005")):
            self.assertEqual(self._one(path).status, "FAIL")

    def test_go_gofmt_and_one_vet_per_module(self) -> None:
        a = self._write("m/a.go", "package m\n")
        b = self._write("m/b.go", "package m\n")
        with mock.patch(WHICH, return_value="/x/go"), mock.patch(RUN, return_value=_proc(0)):
            checks = verify_paths([a, b], str(self.dir))
        self.assertEqual([c.status for c in checks], ["PASS", "PASS", "SKIP"])
        self.assertEqual(checks[2].detail, "no go.mod above an edited file")
        self._write("m/go.mod", "module m\n")
        with mock.patch(WHICH, return_value="/x/go"), mock.patch(RUN, return_value=_proc(0)) as run:
            checks = verify_paths([a, b], str(self.dir))
        self.assertEqual([c.name for c in checks],
                         [f"gofmt -l {a}", f"gofmt -l {b}", f"go vet ./... in {self.dir / 'm'}"])
        self.assertEqual(run.call_count, 3)
        self.assertEqual(run.call_args.args[0], ["go", "vet", "./..."])
        with mock.patch(WHICH, return_value="/x/go"), mock.patch(RUN, side_effect=[_proc(0, "a.go\n"), _proc(1, "", "vet: bad")]):
            checks = verify_paths([a], str(self.dir))
        self.assertEqual([c.status for c in checks], ["FAIL", "FAIL"])
        self.assertIn("not gofmt-formatted", checks[0].detail)
        self.assertIn("vet: bad", checks[1].detail)

    def test_rs_cargo_check_per_crate(self) -> None:
        path = self._write("crate/src/lib.rs", "fn a() {}\n")
        self.assertEqual(self._one(path).detail, "no Cargo.toml above an edited file")
        self._write("crate/Cargo.toml", "[package]\nname = 'c'\n")
        self._skips_without(path, "cargo")
        with mock.patch(WHICH, return_value="/x/cargo"), mock.patch(RUN, return_value=_proc(0)) as run:
            good = self._one(path)
        self.assertEqual((good.status, good.name), ("PASS", f"cargo check in {self.dir / 'crate'}"))
        self.assertEqual(run.call_args.kwargs["cwd"], str(self.dir / "crate"))
        with mock.patch(WHICH, return_value="/x/cargo"), mock.patch(RUN, return_value=_proc(101, "", "error[E0425]")):
            self.assertIn("E0425", self._one(path).detail)

    # ── edges ────────────────────────────────────────────────────

    def test_timeout_is_a_skip_with_reason(self) -> None:
        path = self._write("slow.py", "x = 1\n")
        with mock.patch(RUN, side_effect=subprocess.TimeoutExpired("py", 1)):
            check = self._one(path)
        self.assertEqual(check.status, "SKIP")
        self.assertIn("timed out", check.detail)

    def test_unknown_suffix_and_missing_file(self) -> None:
        check = self._one(self._write("notes.md", "# hi (:\n"))
        self.assertEqual((check.status, check.detail), ("SKIP", "no verifier for .md"))
        self.assertEqual(verify_paths([str(self.dir / "gone.py")], str(self.dir)), [])

    def test_cwd_defaults_to_process_cwd(self) -> None:
        path = self._write("ok.py", "x = 1\n")
        self.assertEqual(verifiers.verify_paths([path])[0].status, "PASS")


if __name__ == "__main__":
    unittest.main()
