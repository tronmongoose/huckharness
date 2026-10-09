"""Tests for serve_beads: beads dir resolution, bd argv per action, id and body checks.

Every bd call is mocked; no test runs bd against a real tracker.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from coding_harness.core import settings as settings_mod
from coding_harness.modes import serve_beads


@pytest.fixture
def beads(tmp_path: Path) -> Path:
    """A directory holding an empty .beads."""
    (tmp_path / "tracker" / ".beads").mkdir(parents=True)
    return tmp_path / "tracker"


@pytest.fixture
def bd(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """subprocess.run replaced; answers success with an empty JSON object."""
    run = mock.MagicMock(return_value=subprocess.CompletedProcess([], 0, "{}", ""))
    monkeypatch.setattr(serve_beads.subprocess, "run", run)
    return run


@pytest.fixture
def writes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings enabled, so bead writes are allowed."""
    monkeypatch.delenv("HARNESS_SETTINGS", raising=False)


def test_resolution_prefers_cwd_then_setting(tmp_path: Path, beads: Path) -> None:
    """cwd with .beads wins, then the setting, then nothing."""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert serve_beads.resolve_beads_dir(str(beads), str(bare)) == beads
    assert serve_beads.resolve_beads_dir(str(bare), str(beads)) == beads
    assert serve_beads.resolve_beads_dir(str(bare), None) is None
    assert serve_beads.resolve_beads_dir(str(bare), str(bare)) is None


def test_resolution_expands_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                 beads: Path) -> None:
    """A ~ in beads_dir is expanded."""
    monkeypatch.setenv("HOME", str(tmp_path))
    assert serve_beads.resolve_beads_dir(str(tmp_path / "x"), "~/tracker") == beads


def test_empty_state_names_the_setting(tmp_path: Path, bd: mock.MagicMock) -> None:
    """No beads anywhere: an explicit reason, and bd is never run."""
    status, body = serve_beads.list_work("open", str(tmp_path), None)
    assert status == 200
    assert body["items"] == [] and body["source"] == "none"
    assert "beads_dir" in body["reason"] and body["writable"] is False
    bd.assert_not_called()


def test_list_runs_bd_in_the_beads_dir(tmp_path: Path, beads: Path,
                                       bd: mock.MagicMock) -> None:
    """The list runs bd list in the resolved directory with a timeout."""
    bd.return_value = subprocess.CompletedProcess([], 0, json.dumps(
        [{"id": "sl-a1", "title": "t", "status": "open", "priority": 1,
          "issue_type": "bug", "extra": "dropped"}]), "")
    status, body = serve_beads.list_work("closed", str(tmp_path), str(beads))
    argv = bd.call_args.args[0]
    assert argv[:3] == ["bd", "list", "--json"] and argv[-2:] == ["--status", "closed"]
    assert bd.call_args.kwargs["cwd"] == str(beads)
    assert bd.call_args.kwargs["timeout"] == serve_beads.BD_TIMEOUT_S
    assert "shell" not in bd.call_args.kwargs
    assert body["source"] == "live" and body["items"][0]["id"] == "sl-a1"
    assert "extra" not in body["items"][0]


@pytest.mark.parametrize("action,body,argv", [
    ("claim", {}, ["bd", "update", "sl-a1", "--claim", "--json"]),
    ("close", {"reason": " done "}, ["bd", "close", "sl-a1", "--reason=done", "--json"]),
    ("note", {"text": "-rf"}, ["bd", "update", "sl-a1", "--append-notes=-rf", "--json"]),
])
def test_action_argv(tmp_path: Path, beads: Path, bd: mock.MagicMock, writes_on: None,
                     action: str, body: dict, argv: list) -> None:
    """Each action builds its bd argv; user text rides as --flag=value."""
    status, reply = serve_beads.handle_post(
        f"/v1/beads/sl-a1/{action}", body, str(tmp_path), str(beads))
    assert status == 200 and reply["id"] == "sl-a1"
    assert bd.call_args.args[0] == argv
    assert bd.call_args.kwargs["cwd"] == str(beads)


def test_create_argv(tmp_path: Path, beads: Path, bd: mock.MagicMock,
                     writes_on: None) -> None:
    """Create passes title, priority and type and reports the new id."""
    bd.return_value = subprocess.CompletedProcess([], 0, '{"id": "sl-new"}', "")
    status, reply = serve_beads.handle_post(
        "/v1/beads", {"title": "fix it", "priority": 0, "type": "bug"},
        str(tmp_path), str(beads))
    assert status == 200 and reply["id"] == "sl-new"
    assert bd.call_args.args[0] == ["bd", "create", "--title=fix it", "--priority=0",
                                    "--type=bug", "--json"]


@pytest.mark.parametrize("body", [
    {}, {"title": "  "}, {"title": "x" * 301}, {"title": "t", "priority": 5},
    {"title": "t", "priority": True}, {"title": "t", "type": "story"},
])
def test_create_rejects_bad_bodies(tmp_path: Path, beads: Path, bd: mock.MagicMock,
                                   writes_on: None, body: dict) -> None:
    """A bad create body is a 400 and bd never runs."""
    status, _ = serve_beads.handle_post("/v1/beads", body, str(tmp_path), str(beads))
    assert status == 400
    bd.assert_not_called()


def test_close_needs_a_reason(tmp_path: Path, beads: Path, bd: mock.MagicMock,
                              writes_on: None) -> None:
    """Close without a reason, or with an oversized one, is a 400."""
    for body in ({}, {"reason": ""}, {"reason": "x" * 4001}):
        status, _ = serve_beads.handle_post(
            "/v1/beads/sl-a1/close", body, str(tmp_path), str(beads))
        assert status == 400
    bd.assert_not_called()


@pytest.mark.parametrize("raw", [
    "-rf", "SL-1", "sl", "sl-", "sl-a1;rm", "sl-a%2Fb", "sl-a b", "1a-b", "sl-" + "a" * 70,
])
def test_bad_ids_rejected(tmp_path: Path, beads: Path, bd: mock.MagicMock,
                          writes_on: None, raw: str) -> None:
    """Ids outside the strict pattern are 400 on read and write; bd never runs."""
    assert serve_beads.handle_get(f"/v1/beads/{raw}", {}, str(tmp_path), str(beads))[0] == 400
    assert serve_beads.handle_post(
        f"/v1/beads/{raw}/claim", {}, str(tmp_path), str(beads))[0] == 400
    bd.assert_not_called()


def test_writes_refused_when_settings_off(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                          beads: Path, bd: mock.MagicMock) -> None:
    """HARNESS_SETTINGS=off makes every write a 409."""
    monkeypatch.setenv("HARNESS_SETTINGS", "off")
    for path, body in (("/v1/beads", {"title": "t"}), ("/v1/beads/sl-a1/claim", {}),
                       ("/v1/beads/sl-a1/note", {"text": "n"})):
        assert serve_beads.handle_post(path, body, str(tmp_path), str(beads))[0] == 409
    bd.assert_not_called()


def test_write_without_beads_dir_is_409(tmp_path: Path, bd: mock.MagicMock,
                                        writes_on: None) -> None:
    """With nowhere to run bd, a write is refused with the reason."""
    status, reply = serve_beads.handle_post("/v1/beads/sl-a1/claim", {}, str(tmp_path), None)
    assert status == 409 and "beads_dir" in reply["error"]["message"]


def test_bd_failure_carries_stderr(tmp_path: Path, beads: Path, bd: mock.MagicMock,
                                   writes_on: None) -> None:
    """A bd error is a 502 with its stderr; a missing issue is a 404."""
    bd.return_value = subprocess.CompletedProcess([], 1, "", "already claimed by bob")
    status, reply = serve_beads.handle_post(
        "/v1/beads/sl-a1/claim", {}, str(tmp_path), str(beads))
    assert status == 502 and "already claimed" in reply["error"]["message"]
    bd.return_value = subprocess.CompletedProcess([], 1, "", "Error: issue sl-a1 not found")
    assert serve_beads.handle_get("/v1/beads/sl-a1", {}, str(tmp_path), str(beads))[0] == 404


def test_bd_timeout_is_502(tmp_path: Path, beads: Path, bd: mock.MagicMock) -> None:
    """A hung bd answers 502 rather than holding the request."""
    bd.side_effect = subprocess.TimeoutExpired(["bd"], 10)
    status, reply = serve_beads.handle_get("/v1/beads/sl-a1", {}, str(tmp_path), str(beads))
    assert status == 502 and "timed out" in reply["error"]["message"]


def test_show_trims_detail(tmp_path: Path, beads: Path, bd: mock.MagicMock) -> None:
    """Show runs bd show and keeps the drawer's fields and trimmed deps."""
    bd.return_value = subprocess.CompletedProcess([], 0, json.dumps([{
        "id": "sl-a1", "title": "t", "notes": "n", "status": "open",
        "dependencies": [{"id": "sl-b2", "title": "dep", "status": "closed",
                          "description": "long", "dependency_type": "blocks"}]}]), "")
    status, detail = serve_beads.handle_get("/v1/beads/sl-a1", {}, str(tmp_path), str(beads))
    assert bd.call_args.args[0] == ["bd", "show", "sl-a1", "--json"]
    assert status == 200 and detail["notes"] == "n"
    assert detail["dependencies"] == [{"id": "sl-b2", "title": "dep", "status": "closed",
                                       "priority": None, "issue_type": None,
                                       "dependency_type": "blocks"}]


def test_unknown_routes_fall_through(tmp_path: Path) -> None:
    """Paths this module does not own answer None; an unknown action is 404."""
    assert serve_beads.handle_get("/v1/other", {}, str(tmp_path), None) is None
    assert serve_beads.handle_post("/v1/other", {}, str(tmp_path), None) is None
    assert serve_beads.handle_post("/v1/beads/sl-a1/delete", {}, str(tmp_path), None)[0] == 404


def test_beads_dir_setting_is_privileged(tmp_path: Path) -> None:
    """beads_dir validates as a string and an untrusted project may not set it."""
    assert "beads_dir" in settings_mod.PRIVILEGED_KEYS
    path = tmp_path / "settings.json"
    assert settings_mod.validate_file({"beads_dir": "~/x"}, path).beads_dir == "~/x"
    with pytest.raises(settings_mod.SettingsError):
        settings_mod.validate_file({"beads_dir": 3}, path)
