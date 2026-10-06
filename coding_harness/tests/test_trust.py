"""Per-project trust (``core.trust``) and the ``bjorn trust`` command with its startup notice."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from coding_harness import cli
from coding_harness.core import settings as settings_mod
from coding_harness.core import trust
from coding_harness.security import hook_adapter


@pytest.fixture
def trust_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The trust gate switched on, reading a trust file under tmp_path."""
    path = tmp_path / "cfg" / "trusted_projects.json"
    monkeypatch.delenv("HARNESS_TRUST_PROJECTS", raising=False)
    monkeypatch.setattr(trust, "TRUST_FILE", path)
    return path


def _dir(tmp_path: Path, rel: str) -> Path:
    """A fresh real directory under tmp_path."""
    path = Path(os.path.realpath(str(tmp_path))) / rel
    path.mkdir(parents=True)
    return path


def test_untrusted_by_default(trust_file: Path, tmp_path: Path) -> None:
    assert trust.trusted_roots() == []
    assert not trust.is_trusted(_dir(tmp_path, "a/proj"))
    assert not trust_file.exists()


def test_root_covers_subdirectories_only(trust_file: Path, tmp_path: Path) -> None:
    proj = _dir(tmp_path, "a/proj")
    sub = _dir(tmp_path, "a/proj/src/deep")
    sibling = _dir(tmp_path, "a/other")
    assert trust.trust(proj) == proj
    assert trust.is_trusted(proj) and trust.is_trusted(sub)
    assert trust.is_trusted(proj / "not-created-yet")
    assert not trust.is_trusted(sibling)
    assert not trust.is_trusted(proj.parent)
    assert not trust.is_trusted(Path("/"))


def test_prefix_lookalike_sibling_is_not_trusted(trust_file: Path, tmp_path: Path) -> None:
    proj = _dir(tmp_path, "a/proj")
    evil = _dir(tmp_path, "a/proj-evil")
    trust.trust(proj)
    assert not trust.is_trusted(evil)
    assert not trust.is_trusted(evil / "sub")


def test_dotdot_cannot_climb_out_of_a_trusted_root(trust_file: Path, tmp_path: Path) -> None:
    proj = _dir(tmp_path, "a/proj")
    _dir(tmp_path, "a/other")
    trust.trust(proj)
    assert not trust.is_trusted(proj / ".." / "other")


def test_symlink_into_trusted_root_is_trusted(trust_file: Path, tmp_path: Path) -> None:
    proj = _dir(tmp_path, "a/proj")
    link = proj.parent / "link"
    link.symlink_to(proj, target_is_directory=True)
    trust.trust(proj)
    assert trust.is_trusted(link)


def test_symlink_inside_trusted_root_pointing_out_is_not_trusted(trust_file: Path, tmp_path: Path) -> None:
    proj = _dir(tmp_path, "a/proj")
    outside = _dir(tmp_path, "a/outside")
    (proj / "escape").symlink_to(outside, target_is_directory=True)
    trust.trust(proj)
    assert not trust.is_trusted(proj / "escape")


def test_trusting_through_a_symlink_stores_the_real_path(trust_file: Path, tmp_path: Path) -> None:
    proj = _dir(tmp_path, "a/proj")
    link = proj.parent / "link"
    link.symlink_to(proj, target_is_directory=True)
    assert trust.trust(link) == proj
    assert json.loads(trust_file.read_text()) == [str(proj)]
    assert trust.is_trusted(proj) and trust.is_trusted(link)


@pytest.mark.parametrize("content", [
    "not json", "", "{\"/\": true}", "\"/\"", "null", "42",
    "[\"relative/dir\", \".\", 7, null, [\"/\"]]",
])
def test_malformed_trust_file_trusts_nothing(trust_file: Path, tmp_path: Path, content: str) -> None:
    trust_file.parent.mkdir(parents=True)
    trust_file.write_text(content)
    assert trust.trusted_roots() == []
    assert not trust.is_trusted(_dir(tmp_path, "a/proj"))
    assert not trust.is_trusted(Path("/"))


def test_trust_file_is_0600_and_idempotent(trust_file: Path, tmp_path: Path) -> None:
    proj = _dir(tmp_path, "a/proj")
    other = _dir(tmp_path, "b")
    trust.trust(proj)
    trust.trust(proj)
    trust.trust(other)
    assert stat.S_IMODE(trust_file.stat().st_mode) == 0o600
    assert json.loads(trust_file.read_text()) == sorted([str(proj), str(other)])
    assert [p.name for p in trust_file.parent.iterdir()] == [trust_file.name]


def test_trust_replaces_a_wider_mode_file_with_0600(trust_file: Path, tmp_path: Path) -> None:
    trust_file.parent.mkdir(parents=True)
    trust_file.write_text("[]")
    trust_file.chmod(0o644)
    trust.trust(_dir(tmp_path, "a/proj"))
    assert stat.S_IMODE(trust_file.stat().st_mode) == 0o600


def test_env_all_trusts_everything_and_other_values_do_not(
    trust_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    proj = _dir(tmp_path, "a/proj")
    for value in ("1", "true", "ALL", "all ", ""):
        monkeypatch.setenv("HARNESS_TRUST_PROJECTS", value)
        assert not trust.is_trusted(proj), value
    monkeypatch.setenv("HARNESS_TRUST_PROJECTS", "all")
    assert trust.is_trusted(proj)
    assert not trust_file.exists()


def test_bjorn_trust_records_the_directory(trust_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    proj = _dir(tmp_path, "a/proj")
    assert cli.main(["trust", str(proj)]) == 0
    assert trust.is_trusted(proj)
    assert f"trusted: {proj}" in capsys.readouterr().out


def test_bjorn_trust_defaults_to_cwd(trust_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj = _dir(tmp_path, "a/proj")
    monkeypatch.chdir(proj)
    assert cli._run_trust([]) == 0
    assert trust.trusted_roots() == [proj]


def test_bjorn_trust_refuses_a_non_directory(trust_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    file = _dir(tmp_path, "a") / "file.txt"
    file.write_text("x")
    assert cli._run_trust([str(file)]) == 2
    assert cli._run_trust([str(tmp_path / "missing")]) == 2
    assert "not a directory" in capsys.readouterr().err
    assert not trust_file.exists()


def _hostile_project(tmp_path: Path) -> Path:
    """A repo carrying privileged settings, an .mcp.json and its own sentinel hook."""
    proj = _dir(tmp_path, "clone")
    (proj / ".git").mkdir()
    (proj / ".bjorn").mkdir()
    (proj / ".bjorn" / "settings.json").write_text(json.dumps(
        {"autonomy": "high", "hooks": {"SessionStart": []}, "denyWrite": ["x"]}))
    (proj / ".mcp.json").write_text(json.dumps({"mcpServers": {}}))
    hook = proj / hook_adapter.HOOK_REL
    hook.parent.mkdir(parents=True)
    hook.write_text("raise SystemExit(0)\n")
    return proj


def test_notice_names_skipped_keys_mcp_and_hook(
    trust_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    proj = _hostile_project(tmp_path)
    monkeypatch.setattr(settings_mod, "USER_SETTINGS", tmp_path / "cfg" / "settings.json")
    monkeypatch.delenv("HARNESS_SETTINGS", raising=False)
    monkeypatch.chdir(proj)
    cli._trust_notice(settings_mod.load_settings(str(proj)))
    err = capsys.readouterr().err
    assert "settings keys autonomy, hooks" in err
    assert "denyWrite" not in err
    assert str(proj / ".mcp.json") in err
    assert str(proj / hook_adapter.HOOK_REL) in err
    assert "bjorn trust" in err


def test_notice_is_silent_once_trusted(
    trust_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    proj = _hostile_project(tmp_path)
    monkeypatch.setattr(settings_mod, "USER_SETTINGS", tmp_path / "cfg" / "settings.json")
    monkeypatch.delenv("HARNESS_SETTINGS", raising=False)
    monkeypatch.chdir(proj)
    trust.trust(proj)
    cli._trust_notice(settings_mod.load_settings(str(proj)))
    assert capsys.readouterr().err == ""


def test_notice_is_silent_for_a_plain_directory(
    trust_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture,
) -> None:
    monkeypatch.chdir(_dir(tmp_path, "plain"))
    cli._trust_notice(settings_mod.Settings())
    assert capsys.readouterr().err == ""
