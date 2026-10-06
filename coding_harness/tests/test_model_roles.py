"""Model roles: defaults, settings, env overrides, fallbacks, and where each role is used."""
from __future__ import annotations

import json
from unittest import mock

import pytest

from coding_harness.core import model_roles
from coding_harness.core.settings import Settings, SettingsError, load_settings
from coding_harness.models.ollama import BannedModelError


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.delenv("HARNESS_REVIEW_MODEL", raising=False)
    monkeypatch.setattr(model_roles, "_present", lambda tag: True)
    model_roles.configure(Settings())
    yield
    model_roles.configure(Settings())


def test_defaults_split_one_big_and_one_small() -> None:
    assert model_roles.role_model("code") == "mistral-small3.2:latest"
    assert model_roles.role_model("review") == "mistral-small3.2"
    assert {model_roles.role_model(r) for r in ("chat", "explore", "summarize")} == {model_roles.SMALL}


def test_settings_assign_roles_and_the_legacy_model_key_means_code() -> None:
    model_roles.configure(Settings(model="gpt-oss:20b", models={"chat": "nemotron-3-nano:4b"}))
    assert model_roles.role_model("code") == "gpt-oss:20b"
    assert model_roles.role_model("chat") == "nemotron-3-nano:4b"
    model_roles.configure(Settings(model="gpt-oss:20b", models={"code": "gemma4:26b"}))
    assert model_roles.role_model("code") == "gemma4:26b"


def test_env_review_override_still_wins(monkeypatch) -> None:
    model_roles.configure(Settings(models={"review": "gemma4:26b"}))
    monkeypatch.setenv("HARNESS_REVIEW_MODEL", "gpt-oss:20b")
    assert model_roles.role_model("review") == "gpt-oss:20b"
    sources = {r["role"]: r["source"] for r in model_roles.table()}
    assert sources["review"] == "env HARNESS_REVIEW_MODEL" and sources["chat"] == "default"


def test_a_missing_default_falls_back_to_the_code_model(monkeypatch) -> None:
    monkeypatch.setattr(model_roles, "_present", lambda tag: tag != model_roles.SMALL)
    assert model_roles.role_model("summarize") == "mistral-small3.2:latest"


def test_banned_origins_and_unknown_roles_are_refused() -> None:
    model_roles.configure(Settings(models={"chat": "qwen2.5:7b"}))
    with pytest.raises(BannedModelError):
        model_roles.role_model("chat")
    with pytest.raises(ValueError):
        model_roles.role_model("planner")


def test_settings_file_validates_the_models_block(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_SETTINGS", "on")
    user = tmp_path / "user.json"
    monkeypatch.setattr("coding_harness.core.settings.USER_SETTINGS", user)
    user.write_text(json.dumps({"models": {"chat": "granite4.1:8b"}}))
    assert load_settings(str(tmp_path)).models == {"chat": "granite4.1:8b"}
    user.write_text(json.dumps({"models": {"planner": "x"}}))
    with pytest.raises(SettingsError, match="unknown model role"):
        load_settings(str(tmp_path))


def test_compaction_and_proposals_use_the_small_roles() -> None:
    from coding_harness.core import memory_proposals
    from coding_harness.core.session import Session
    from coding_harness.tools.registry import ToolRegistry

    with mock.patch.object(memory_proposals.review, "_tag_present", return_value=True):
        assert memory_proposals.generator_model("coder") == model_roles.SMALL
    s = Session(model="mistral-small3.2:latest", registry=ToolRegistry(event_sink=None),
                system_prompt="x")
    assert s._summary_model() == model_roles.SMALL
