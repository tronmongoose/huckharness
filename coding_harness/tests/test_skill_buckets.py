"""Skill buckets: the user map file, precedence, list_skills counts, save_skill categories."""
from __future__ import annotations

import json
from unittest import mock

import pytest

from coding_harness.context import skill_buckets, skills
from coding_harness.modes import serve_gui


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A tmp HOME so USER_MAP never resolves into the real config tree."""
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HOME", str(root))
    return root


def _map_file(home):
    """The map path under ``home``, with its parent created."""
    path = home / ".config" / "bjorn" / "skill_buckets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _write_map(home, data):
    """Write ``data`` as JSON to the map path under ``home``."""
    _map_file(home).write_text(json.dumps(data), encoding="utf-8")


def test_user_map_resolves_under_home(home):
    assert skill_buckets.USER_MAP.expanduser() == _map_file(home)


def test_user_buckets_is_empty_without_a_file(home):
    assert skill_buckets.user_buckets() == {}


@pytest.mark.parametrize("text", ["{not json", "", "\x00\x01"])
def test_user_buckets_is_empty_for_malformed_json(home, text):
    _map_file(home).write_text(text, encoding="utf-8")
    assert skill_buckets.user_buckets() == {}


def test_user_buckets_is_empty_for_undecodable_bytes(home):
    _map_file(home).write_bytes(b"\xff\xfe{")
    assert skill_buckets.user_buckets() == {}


def test_user_buckets_is_empty_when_the_path_is_a_directory(home):
    _map_file(home).mkdir()
    assert skill_buckets.user_buckets() == {}


@pytest.mark.parametrize("data", [["budget"], "budget", 3, None])
def test_user_buckets_is_empty_for_non_object_json(home, data):
    _write_map(home, data)
    assert skill_buckets.user_buckets() == {}


def test_user_buckets_ignores_unknown_buckets(home):
    _write_map(home, {"Gardening": ["compost"], "Fleet & ops": ["triage"]})
    assert skill_buckets.user_buckets() == {"triage": "Fleet & ops"}


def test_user_buckets_canonicalises_bucket_names(home):
    _write_map(home, {" money & HOUSEHOLD ": ["budget"], "inbox & comms": ["triage"]})
    assert skill_buckets.user_buckets() == {
        "budget": "Money & household", "triage": "Inbox & comms"}


@pytest.mark.parametrize("value", ["budget", {"budget": 1}, 7, None])
def test_user_buckets_ignores_non_list_values(home, value):
    _write_map(home, {"Money & household": value, "Design & media": ["sketch"]})
    assert skill_buckets.user_buckets() == {"sketch": "Design & media"}


def test_bucket_of_precedence_through_the_map_file(home):
    _write_map(home, {"Money & household": ["budget"]})
    assert skill_buckets.bucket_of("budget") == "Money & household"
    assert skill_buckets.bucket_of("budget", "fleet & OPS") == "Fleet & ops"
    assert skill_buckets.bucket_of("budget", "not a bucket") == "Money & household"
    assert skill_buckets.bucket_of("unmapped") == "Other"
    assert skill_buckets.bucket_of("unmapped", " design & media ") == "Design & media"


def test_bucket_of_prefers_an_explicit_by_name_over_the_file(home):
    _write_map(home, {"Money & household": ["ledger"]})
    by_name = {"ledger": "Briefs & digests"}
    assert skill_buckets.bucket_of("ledger", by_name=by_name) == "Briefs & digests"
    assert skill_buckets.bucket_of("ledger", by_name={}) == "Other"
    assert skill_buckets.bucket_of("ledger", "Fleet & ops", by_name) == "Fleet & ops"


def test_no_map_file_puts_every_uncategorised_skill_in_other(home):
    for name in ("budget", "ledger", "triage", "sketch", "deploy", "session-close"):
        assert skill_buckets.bucket_of(name) == "Other"


def test_package_ships_no_skill_name_map():
    for attr in ("DEFAULT_BUCKETS", "_DEFAULT_GROUPS"):
        assert not hasattr(skill_buckets, attr)
    known = {b.lower() for b in skill_buckets.BUCKETS}
    for name, value in vars(skill_buckets).items():
        if isinstance(value, dict) and not name.startswith("__"):
            assert set(value) <= known, name


def test_index_reads_category_from_frontmatter(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    pack = project / ".bjorn" / "skills" / "triage"
    pack.mkdir(parents=True)
    (pack / "SKILL.md").write_text(
        "---\nname: triage\ndescription: sort\ncategory: Inbox & comms\n---\nBody.\n")
    (found,) = skills.index_skills(str(project))
    assert found.category == "Inbox & comms"
    assert "category" not in skills.skills_block(str(project)).lower()


def _fake(rows):
    """Patch the skill index to return one Skill per (name, category) row."""
    return mock.patch.object(skills, "index_skills", return_value=[
        skills.Skill(name, "d", serve_gui.Path("/nowhere") / name / "SKILL.md", category)
        for name, category in rows])


def test_list_skills_buckets_in_order_with_other_last(home):
    _write_map(home, {"Money & household": ["budget", "ledger"], "inbox & comms": ["triage"]})
    rows = [("budget", ""), ("ledger", ""), ("triage", ""), ("odd", ""), ("x", "Fleet & ops")]
    with _fake(rows):
        out = serve_gui.list_skills("/nowhere")
    assert [s["bucket"] for s in out["skills"]] == [
        "Money & household", "Money & household", "Inbox & comms", "Other", "Fleet & ops"]
    names = [b["name"] for b in out["buckets"]]
    assert names == [*skill_buckets.BUCKETS, "Other"]
    counts = {b["name"]: b["count"] for b in out["buckets"]}
    assert (counts["Money & household"], counts["Fleet & ops"], counts["Other"]) == (2, 1, 1)
    assert counts["Design & media"] == 0


def test_list_skills_reads_the_map_once_per_call(home):
    _write_map(home, {"Money & household": ["budget"]})
    with _fake([("budget", ""), ("ledger", ""), ("triage", "")]), mock.patch.object(
            skill_buckets, "user_buckets", wraps=skill_buckets.user_buckets) as spy:
        out = serve_gui.list_skills("/nowhere")
    assert spy.call_count == 1
    assert [s["bucket"] for s in out["skills"]] == ["Money & household", "Other", "Other"]


def test_list_skills_without_a_map_puts_everything_uncategorised_in_other(home):
    with _fake([("budget", ""), ("ledger", ""), ("triage", "Inbox & comms")]):
        out = serve_gui.list_skills("/nowhere")
    assert [s["bucket"] for s in out["skills"]] == ["Other", "Other", "Inbox & comms"]
    counts = {b["name"]: b["count"] for b in out["buckets"]}
    assert (counts["Other"], counts["Inbox & comms"], counts["Money & household"]) == (2, 1, 0)


def test_list_skills_omits_an_empty_other(home):
    _write_map(home, {"Money & household": ["budget"]})
    with _fake([("budget", "")]):
        out = serve_gui.list_skills("/nowhere")
    assert [b["name"] for b in out["buckets"]] == list(skill_buckets.BUCKETS)


def test_save_skill_writes_a_valid_category_and_rejects_others(tmp_path):
    with mock.patch.object(skills, "SKILLS_USER", tmp_path / "bjorn"):
        ok, path = serve_gui.save_skill("triage", "sort", "Body.", "inbox & COMMS")
        assert ok
        text = serve_gui.Path(path).read_text()
        assert "category: Inbox & comms\n" in text
        ok, reason = serve_gui.save_skill("bad", "sort", "Body.", "Gardening")
        assert (ok, reason) == (False, "category must name a skill bucket")
        assert not (tmp_path / "bjorn" / "bad").exists()
        ok, path = serve_gui.save_skill("plain", "sort", "Body.")
        assert ok and "category" not in serve_gui.Path(path).read_text()
