"""The GUI settings routes: read both files, write only the user file's writable keys."""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from coding_harness.core import settings as settings_mod
from coding_harness.core.mode import Autonomy
from coding_harness.modes import serve_settings

BRAIN = {"command": "recall", "args": [], "env": {"TOKEN": "hunter2"}}


@pytest.fixture
def files(tmp_path, monkeypatch):
    """A user file with a brain block and a project with its own settings file."""
    user = tmp_path / "home" / "settings.json"
    user.parent.mkdir()
    user.write_text(json.dumps({"brain": BRAIN, "hooks": {"PreToolUse": []},
                                "sandbox": {"network": False}, "model": "gemma3:12b"}))
    project = tmp_path / "repo"
    (project / ".git").mkdir(parents=True)
    (project / ".bjorn").mkdir()
    proj_file = project / ".bjorn" / "settings.json"
    proj_file.write_text(json.dumps({"denyWrite": ["secrets/**"]}))
    monkeypatch.setattr(settings_mod, "USER_SETTINGS", user)
    monkeypatch.delenv("HARNESS_SETTINGS", raising=False)
    return user, project, proj_file


def _put(body: dict, cwd: Path, applied: list | None = None):
    """PUT through the route function, collecting what it applies."""
    sink = applied if applied is not None else []
    return serve_settings.put_settings(body, str(cwd), sink.append)


def test_get_shape_redacts_brain_env(files) -> None:
    user, project, proj_file = files
    status, body = serve_settings.get_settings(str(project))
    assert status == 200
    assert body["locked"] == ["brain", "hooks"]
    assert body["user_path"] == str(user) and body["project_path"] == str(proj_file)
    assert body["project"] == {"denyWrite": ["secrets/**"]}
    assert body["user"]["brain"]["env"] == {"TOKEN": "***"}
    assert body["effective"]["model"] == "gemma3:12b"
    assert body["effective"]["denyWrite"] == ["secrets/**"]
    assert "hunter2" not in json.dumps(body)


def test_put_writes_only_writable_keys_and_keeps_locked(files) -> None:
    user, project, proj_file = files
    before = proj_file.read_text()
    status, body = _put({"user": {"autonomy": "medium", "model": "gpt-oss:20b",
                                  "commandAllowlist": ["make test"]}}, project)
    assert status == 200, body
    written = json.loads(user.read_text())
    assert written == {"brain": BRAIN, "hooks": {"PreToolUse": []},
                       "sandbox": {"network": False}, "autonomy": "medium",
                       "model": "gpt-oss:20b", "commandAllowlist": ["make test"]}
    assert proj_file.read_text() == before
    assert stat.S_IMODE(user.stat().st_mode) == 0o600
    assert [p.name for p in user.parent.iterdir()] == ["settings.json"], "no temp left"


def test_put_omitting_a_writable_key_clears_it(files) -> None:
    user, project, _ = files
    assert _put({"user": {}}, project)[0] == 200
    assert "model" not in json.loads(user.read_text())


@pytest.mark.parametrize("key", ["brain", "hooks"])
def test_locked_keys_are_rejected(files, key) -> None:
    user, project, _ = files
    before = user.read_text()
    status, body = _put({"user": {key: {}}}, project)
    assert status == 400 and "by hand" in body["error"]["message"]
    assert user.read_text() == before


def test_unknown_and_invalid_values_are_400_with_loader_message(files) -> None:
    user, project, _ = files
    before = user.read_text()
    assert _put({"user": {"colour": "red"}}, project)[0] == 400
    status, body = _put({"user": {"denyWrite": "not-a-list"}}, project)
    assert status == 400 and "denyWrite must be a list of strings" in body["error"]["message"]
    status, body = _put({"user": {"autonomy": "reckless"}}, project)
    assert status == 400 and str(user) in body["error"]["message"]
    assert _put({"user": "x"}, project)[0] == 400
    assert user.read_text() == before


def test_banned_model_is_refused(files) -> None:
    user, project, _ = files
    before = user.read_text()
    status, body = _put({"user": {"model": "qwen2.5:7b"}}, project)
    assert status == 400 and "banned" in body["error"]["message"]
    assert user.read_text() == before


def test_sandbox_is_not_writable_and_is_kept(files) -> None:
    user, project, _ = files
    before = user.read_text()
    status, _ = _put({"user": {"sandbox": {"brain": {"command": "x"}}}}, project)
    assert status == 400 and user.read_text() == before
    assert _put({"user": {"autonomy": "low"}}, project)[0] == 200
    assert json.loads(user.read_text())["sandbox"] == {"network": False}


def test_lists_are_capped(files) -> None:
    user, project, _ = files
    before = user.read_text()
    status, body = _put({"user": {"denyWrite": ["x"] * 501}}, project)
    assert status == 400 and "500 entries" in body["error"]["message"]
    status, body = _put({"user": {"commandAllowlist": ["y" * 1025]}}, project)
    assert status == 400 and "1024 characters" in body["error"]["message"]
    assert user.read_text() == before
    assert _put({"user": {"denyWrite": ["x"] * 500}}, project)[0] == 200


def test_put_is_refused_while_settings_are_off(files, monkeypatch) -> None:
    user, project, _ = files
    monkeypatch.setenv("HARNESS_SETTINGS", "off")
    before = user.read_text()
    status, body = _put({"user": {"autonomy": "low"}}, project)
    assert status == 409 and body["error"]["message"] == "settings disabled"
    assert user.read_text() == before
    assert serve_settings.get_settings(str(project))[1]["disabled"] is True


def test_saved_settings_apply_to_new_sessions(files) -> None:
    _, project, _ = files
    applied: list = []
    assert _put({"user": {"autonomy": "high"}}, project, applied)[0] == 200
    assert applied and applied[0].autonomy is Autonomy.HIGH
    assert applied[0].deny_write == ["secrets/**"]


def test_first_save_creates_the_user_file(tmp_path, monkeypatch) -> None:
    user = tmp_path / "fresh" / "bjorn" / "settings.json"
    monkeypatch.setattr(settings_mod, "USER_SETTINGS", user)
    monkeypatch.delenv("HARNESS_SETTINGS", raising=False)
    assert _put({"user": {"autonomy": "off"}}, tmp_path)[0] == 200
    assert json.loads(user.read_text()) == {"autonomy": "off"}
    assert stat.S_IMODE(user.stat().st_mode) == 0o600


@pytest.fixture(scope="module")
def server_url():
    """A real serve process on a free port."""
    from coding_harness.tests.test_serve_mode import _start_server
    server, thread, port = _start_server()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()
    server.server_close()
    thread.join(timeout=2.0)


def test_routes_are_wired_and_described(files, server_url) -> None:
    from coding_harness.tests.test_serve_mode import _request
    status, body = _request("GET", f"{server_url}/v1/settings")
    assert status == 200 and json.loads(body)["locked"] == ["brain", "hooks"]
    status, _ = _request("PUT", f"{server_url}/v1/settings", body={"user": {"brain": {}}})
    assert status == 400
    status, _ = _request("PUT", f"{server_url}/v1/settings", body={"user": {"autonomy": "low"}})
    assert status == 200
    status, body = _request("GET", f"{server_url}/v1/transcripts?q=nothing-matches-this")
    assert status == 200 and json.loads(body) == {"transcripts": []}
    assert _request("POST", f"{server_url}/v1/transcripts/hx-none/delete", body={})[0] == 404
    assert _request("POST", f"{server_url}/v1/transcripts/bad.id/title", body={})[0] == 400
    spec = json.loads(_request("GET", f"{server_url}/v1/openapi.json")[1])
    assert {"/v1/settings", "/v1/transcripts/{id}/delete"} <= set(spec["paths"])


def test_put_saves_role_models_and_refuses_a_banned_one(files) -> None:
    user, project, _ = files
    status, body = _put({"user": {"models": {"chat": "nemotron-3-nano:4b"}}}, project)
    assert status == 200, body
    assert json.loads(user.read_text())["models"] == {"chat": "nemotron-3-nano:4b"}
    assert body["effective"]["models"] == {"chat": "nemotron-3-nano:4b"}
    status, body = _put({"user": {"models": {"explore": "qwen2.5:7b"}}}, project)
    assert status == 400
