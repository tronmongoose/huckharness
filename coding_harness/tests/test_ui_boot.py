"""The GUI child's boot string: an untrusted cwd is never an import root.

Each test runs the real ``ui_mode._BOOT`` prefix (everything before it imports
the CLI) under ``python -c`` in a hostile working directory, then imports the
package a deployment would supply. HOME points at a temp directory so the child
reads a trust file the test wrote, never the developer's.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coding_harness.modes import ui_mode

REPO_ROOT = Path(__file__).resolve().parents[2]
HOSTILE = "import pathlib\npathlib.Path(__file__).resolve().parents[1].joinpath('{name}.marker').write_text('x')\n"


def _boot_then(statement: str) -> str:
    """``_BOOT`` cut before it imports the CLI, followed by ``statement``."""
    prefix, cut, _ = ui_mode._BOOT.partition("from coding_harness.cli")
    assert cut and "trust.is_trusted" in prefix
    return prefix + statement


@pytest.fixture
def hostile(tmp_path: Path) -> tuple[Path, Path]:
    """(cwd, fake home): a cwd holding hostile ``pipelines`` and ``coding_harness`` packages."""
    base = Path(os.path.realpath(str(tmp_path)))
    cwd = base / "clone"
    for name in ("pipelines", "coding_harness"):
        (cwd / name).mkdir(parents=True)
        (cwd / name / "__init__.py").write_text(HOSTILE.format(name=name))
    home = base / "home"
    home.mkdir()
    return cwd, home


def _run(code: str, cwd: Path, home: Path, **extra: str) -> subprocess.CompletedProcess:
    """Run ``code`` with ``python -c`` in ``cwd`` with the worktree on PYTHONPATH."""
    env = {k: v for k, v in os.environ.items() if k != "HARNESS_TRUST_PROJECTS"}
    env.update({"PYTHONPATH": str(REPO_ROOT), "HOME": str(home), **extra})
    return subprocess.run([sys.executable, "-c", code], cwd=str(cwd), env=env,
                          capture_output=True, text=True, timeout=60)


def _trust(home: Path, directory: Path) -> None:
    """Write the trust file the child will read under its fake HOME."""
    path = home / ".config" / "bjorn" / "trusted_projects.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps([str(directory)]))


def test_plain_python_c_would_import_the_hostile_package(hostile: tuple[Path, Path]) -> None:
    cwd, home = hostile
    proc = _run("import pipelines", cwd, home)
    assert proc.returncode == 0, proc.stderr
    assert (cwd / "pipelines.marker").exists()


def test_untrusted_cwd_is_not_importable(hostile: tuple[Path, Path]) -> None:
    cwd, home = hostile
    proc = _run(_boot_then("import pipelines"), cwd, home)
    assert not (cwd / "pipelines.marker").exists()
    assert not (cwd / "coding_harness.marker").exists()
    assert proc.returncode != 0 and "ModuleNotFoundError" in proc.stderr


def test_untrusted_cwd_resolves_coding_harness_to_the_installed_package(hostile: tuple[Path, Path]) -> None:
    cwd, home = hostile
    proc = _run(_boot_then("import coding_harness; print(coding_harness.__file__)"), cwd, home)
    assert proc.returncode == 0, proc.stderr
    assert Path(proc.stdout.strip()).parent == REPO_ROOT / "coding_harness"
    assert not (cwd / "coding_harness.marker").exists()


def test_trusted_cwd_is_importable(hostile: tuple[Path, Path]) -> None:
    cwd, home = hostile
    _trust(home, cwd)
    proc = _run(_boot_then("import pipelines"), cwd, home)
    assert proc.returncode == 0, proc.stderr
    assert (cwd / "pipelines.marker").exists()


def test_trust_all_env_makes_cwd_importable(hostile: tuple[Path, Path]) -> None:
    cwd, home = hostile
    proc = _run(_boot_then("import pipelines"), cwd, home, HARNESS_TRUST_PROJECTS="all")
    assert proc.returncode == 0, proc.stderr
    assert (cwd / "pipelines.marker").exists()


def test_trusting_a_lookalike_sibling_does_not_expose_cwd(hostile: tuple[Path, Path]) -> None:
    cwd, home = hostile
    _trust(home, cwd.parent / "clone-evil")
    _run(_boot_then("import pipelines"), cwd, home)
    assert not (cwd / "pipelines.marker").exists()


def test_untrusted_cwd_reached_through_a_symlink_is_not_importable(hostile: tuple[Path, Path]) -> None:
    cwd, home = hostile
    link = cwd.parent / "link"
    link.symlink_to(cwd, target_is_directory=True)
    _run(_boot_then("import pipelines"), link, home)
    assert not (cwd / "pipelines.marker").exists()


SYMLINK_SPELLING = pytest.param("{link}", marks=pytest.mark.xfail(
    strict=True, reason="_BOOT compares sys.path entries to os.getcwd() as strings, so a symlinked "
                        "spelling of the cwd on the user's own PYTHONPATH survives the filter"))


@pytest.mark.parametrize("spelling", [".", "", "{cwd}/", "{cwd}/.", SYMLINK_SPELLING])
def test_untrusted_cwd_on_pythonpath_is_not_importable(hostile: tuple[Path, Path], spelling: str) -> None:
    """A PYTHONPATH that names the cwd in another spelling must not put it back."""
    cwd, home = hostile
    link = cwd.parent / "link"
    link.symlink_to(cwd, target_is_directory=True)
    extra = spelling.format(cwd=cwd, link=link)
    _run(_boot_then("import pipelines"), cwd, home, PYTHONPATH=f"{REPO_ROOT}{os.pathsep}{extra}")
    assert not (cwd / "pipelines.marker").exists()


def test_spawn_uses_the_boot_string(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def _popen(argv: list[str], **kwargs: object) -> None:
        seen.update(argv=argv, cwd=kwargs.get("cwd"))

    monkeypatch.setattr(ui_mode.subprocess, "Popen", _popen)
    monkeypatch.setattr(ui_mode, "SPAWN_WAIT_S", 0.0)
    monkeypatch.setattr(ui_mode, "registry_dir", lambda: tmp_path / "gui")
    assert ui_mode.spawn(str(tmp_path), model=None, force_local=False) is None
    assert seen["argv"][:3] == [sys.executable, "-c", ui_mode._BOOT]
    assert "-m" not in seen["argv"] and seen["cwd"] == str(tmp_path)
