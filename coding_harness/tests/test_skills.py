"""Skill index: parsing, precedence, the budgeted block, and load_skill."""
from __future__ import annotations

from coding_harness.context import skills


def _make_skill(root, dirname, text):
    d = root / dirname
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


def _isolate_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def test_frontmatter_name_and_description(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(
        project / ".bjorn" / "skills", "deploy",
        "---\nname: ship-it\ndescription: Deploy the app safely\n---\n\nBody.\n",
    )
    found = skills.index_skills(str(project))
    assert [(s.name, s.description) for s in found] == [
        ("ship-it", "Deploy the app safely"),
    ]


def test_fallbacks_dir_name_and_first_paragraph(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(
        project / ".bjorn" / "skills", "review",
        "# Review checklist\n\nWalk the diff twice\nbefore approving.\n\nMore.\n",
    )
    found = skills.index_skills(str(project))
    assert found[0].name == "review"
    assert found[0].description == "Walk the diff twice before approving."


def test_project_skill_wins_over_user_skill(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(
        home / ".config" / "bjorn" / "skills", "deploy",
        "---\nname: deploy\ndescription: user version\n---\n",
    )
    _make_skill(
        project / ".bjorn" / "skills", "deploy",
        "---\nname: deploy\ndescription: project version\n---\n",
    )
    found = skills.index_skills(str(project))
    assert len(found) == 1
    assert found[0].description == "project version"


def test_skills_block_renders_and_respects_budget(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    root = project / ".bjorn" / "skills"
    for i in range(40):
        _make_skill(
            root, f"skill-{i:02d}",
            f"---\nname: skill-{i:02d}\ndescription: {'d' * 120}\n---\n",
        )
    block = skills.skills_block(str(project))
    assert block.startswith("\n## Skills\n")
    assert "- skill-00:" in block
    assert len(block.encode("utf-8")) <= 2000


def test_skills_block_empty_when_no_skills(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    assert skills.skills_block(str(project)) == ""


def test_folded_block_scalar_description_is_read(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(
        project / ".bjorn" / "skills", "artist",
        "---\nname: artist\ndescription: >\n"
        "  Renaissance advisor holding aesthetic judgment. Use when an\n"
        "  artifact needs review.\ntags: [a, b]\n---\n\nBody.\n",
    )
    found = skills.index_skills(str(project))
    assert found[0].description.startswith("Renaissance advisor holding aesthetic")
    assert ">" not in found[0].description


def test_indented_continuation_is_not_mistaken_for_a_key(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(
        project / ".bjorn" / "skills", "counsel",
        "---\nname: counsel\ndescription: |\n"
        "  Synthesis work. Trigger phrases: research, brief.\n---\n",
    )
    found = skills.index_skills(str(project))
    # 'Trigger phrases:' must stay inside the description, not become a key
    # that silently shadows real metadata.
    assert "Trigger phrases: research, brief." in found[0].description
    assert found[0].name == "counsel"


def test_description_clip_lands_on_a_word_boundary(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    root = project / ".bjorn" / "skills"
    for i in range(30):
        _make_skill(
            root, f"skill-{i:02d}",
            f"---\nname: skill-{i:02d}\n"
            f"description: {'alpha bravo charlie delta echo foxtrot ' * 6}\n---\n",
        )
    block = skills.skills_block(str(project))
    for line in block.splitlines():
        if not line.startswith("- skill-"):
            continue
        assert not line.endswith(("alp", "brav", "charli", "delt", "foxtro"))


def test_shared_convention_roots_are_indexed(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(home / ".claude" / "skills", "voice",
                "---\nname: voice\ndescription: user claude skill\n---\n")
    _make_skill(home / ".agents" / "skills", "agents-one",
                "---\nname: agents-one\ndescription: user agents skill\n---\n")
    _make_skill(project / "skills", "bare",
                "---\nname: bare\ndescription: bare project skills dir\n---\n")
    _make_skill(project / ".claude" / "skills", "proj-claude",
                "---\nname: proj-claude\ndescription: project claude skill\n---\n")
    names = [s.name for s in skills.index_skills(str(project))]
    assert names == ["agents-one", "bare", "proj-claude", "voice"]


def test_native_bjorn_root_wins_over_borrowed_layout(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(project / "skills", "deploy",
                "---\nname: deploy\ndescription: bare version\n---\n")
    _make_skill(project / ".claude" / "skills", "deploy",
                "---\nname: deploy\ndescription: claude version\n---\n")
    _make_skill(project / ".bjorn" / "skills", "deploy",
                "---\nname: deploy\ndescription: bjorn version\n---\n")
    found = skills.index_skills(str(project))
    assert len(found) == 1
    assert found[0].description == "bjorn version"


def test_project_beats_user_across_borrowed_roots(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(home / ".claude" / "skills", "deploy",
                "---\nname: deploy\ndescription: user version\n---\n")
    _make_skill(project / "skills", "deploy",
                "---\nname: deploy\ndescription: project version\n---\n")
    found = skills.index_skills(str(project))
    assert [s.description for s in found] == ["project version"]


def test_directory_without_skill_md_is_not_a_skill(tmp_path, monkeypatch):
    home = _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    # ~/.claude/skills/synced is a real cache directory on this machine.
    (home / ".claude" / "skills" / "synced" / "deadbeef").mkdir(parents=True)
    _make_skill(home / ".claude" / "skills", "real",
                "---\nname: real\ndescription: d\n---\n")
    assert [s.name for s in skills.index_skills(str(project))] == ["real"]


def test_symlinked_skill_directory_is_indexed(tmp_path, monkeypatch):
    """.bjorn/skills entries symlinked at a bare skills/ dir still resolve."""
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    _make_skill(project / "elsewhere", "wish",
                "---\nname: wish\ndescription: linked\n---\n")
    link_root = project / ".bjorn" / "skills"
    link_root.mkdir(parents=True)
    (link_root / "wish").symlink_to(project / "elsewhere" / "wish")
    assert [s.name for s in skills.index_skills(str(project))] == ["wish"]


def test_every_skill_is_named_even_when_descriptions_would_overflow(
        tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    root = project / ".bjorn" / "skills"
    for i in range(40):
        _make_skill(root, f"skill-{i:02d}",
                    f"---\nname: skill-{i:02d}\ndescription: {'d' * 200}\n---\n")
    block = skills.skills_block(str(project))
    assert len(block.encode("utf-8")) <= 2000
    for i in range(40):
        assert f"- skill-{i:02d}" in block, f"skill-{i:02d} was dropped silently"


def test_overflowing_name_list_reports_how_many_it_dropped(
        tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    root = project / ".bjorn" / "skills"
    for i in range(400):
        _make_skill(root, f"skill-{i:03d}",
                    f"---\nname: skill-{i:03d}\ndescription: d\n---\n")
    block = skills.skills_block(str(project))
    assert len(block.encode("utf-8")) <= 2000
    assert "more, ask to list them)" in block


def test_load_skill_returns_full_text(tmp_path, monkeypatch):
    _isolate_home(monkeypatch, tmp_path)
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    text = "---\nname: deploy\ndescription: d\n---\n\nStep 1. Step 2.\n"
    _make_skill(project / ".bjorn" / "skills", "deploy", text)
    assert skills.load_skill("deploy", cwd=str(project)) == text
    assert skills.load_skill("missing", cwd=str(project)) is None
