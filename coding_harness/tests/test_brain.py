"""BrainClient against a fake slos-recall server, and the tier rules."""
from __future__ import annotations

import pytest

from coding_harness.context import brain
from coding_harness.core.settings import SettingsError, load_settings
from coding_harness.tests import fake_brain


@pytest.fixture()
def client(tmp_path):
    c = brain.BrainClient(fake_brain.block(tmp_path))
    yield c
    c.close()


def test_search_returns_hits_with_tiers(client) -> None:
    hits = client.search("larkspur")
    assert {h.path: h.to_dict()["tier"] for h in hits} == {
        "startup/plan.md": 1, "finance/budget.md": 2, "health/visit.md": 3, "life/unlabeled.md": 3}


def test_vault_filter_keeps_one_vault(client) -> None:
    assert [h.path for h in client.search("larkspur", vault="finance")] == ["finance/budget.md"]


def test_page_and_unknown_page(client) -> None:
    assert client.page("finance/budget.md")["sensitivity"] == "confidential"
    with pytest.raises(brain.BrainError, match="not indexed"):
        client.page("nope.md")


def test_client_restarts_after_the_server_dies(client) -> None:
    client.search("larkspur")
    client._client.close()
    assert client.search("launch")[0].path == "startup/plan.md"


def test_unknown_tier_is_restricted_and_frontier_gets_internal() -> None:
    assert brain.tier_of("") == brain.tier_of("secret-ish") == 3
    assert (brain.allowed_tier(local=True), brain.allowed_tier(local=False)) == (3, 1)


def test_unconfigured_brain_has_no_client() -> None:
    assert brain.client_for({}) is None


def test_a_project_settings_file_cannot_set_brain(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_SETTINGS", "on")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".bjorn").mkdir()
    (tmp_path / ".bjorn" / "settings.json").write_text('{"brain": {"command": "evil"}}')
    monkeypatch.setattr("coding_harness.core.settings.USER_SETTINGS", tmp_path / "none.json")
    with pytest.raises(SettingsError, match="brain may only be set"):
        load_settings(str(tmp_path))
