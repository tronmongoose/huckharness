"""Deterministic repo map: ranking, budget, determinism."""
from __future__ import annotations

from coding_harness.context.repomap import build_repo_map


def _repo(tmp_path):
    """A tiny repo: core defines symbols that a, b, c all reference; lonely is
    defined but referenced by nobody."""
    (tmp_path / "core.py").write_text(
        "class Engine:\n    def spin(self):\n        return 1\n\n"
        "def ignite(fuel):\n    return fuel\n"
    )
    for name in ("a", "b", "c"):
        (tmp_path / f"{name}.py").write_text(
            "from core import Engine, ignite\n"
            "e = Engine()\n"
            "ignite(3)\n"
        )
    (tmp_path / "lonely.py").write_text("def obscure_helper():\n    return 0\n")
    return tmp_path


def test_map_renders_and_is_bounded(tmp_path):
    m = build_repo_map(_repo(tmp_path), budget_tokens=1024)
    assert "Repository map" in m
    assert "core.py" in m
    assert "def ignite(fuel)" in m


def test_referenced_file_outranks_unreferenced(tmp_path):
    m = build_repo_map(_repo(tmp_path), budget_tokens=1024)
    # core (referenced by a/b/c) must appear before lonely (referenced by none).
    assert m.index("core.py") < m.index("lonely.py")


def test_budget_is_respected(tmp_path):
    m = build_repo_map(_repo(tmp_path), budget_tokens=8)  # ~32 chars
    # Tiny budget: at most the highest-ranked file's block fits, or nothing.
    assert "lonely.py" not in m


def test_deterministic(tmp_path):
    repo = _repo(tmp_path)
    assert build_repo_map(repo, 1024) == build_repo_map(repo, 1024)


def test_empty_repo_is_blank(tmp_path):
    assert build_repo_map(tmp_path, 1024) == ""


def test_test_files_excluded(tmp_path):
    _repo(tmp_path)
    (tmp_path / "test_core.py").write_text("def test_spin():\n    assert True\n")
    m = build_repo_map(tmp_path, 1024)
    assert "test_core.py" not in m


def test_class_signature_lists_public_methods(tmp_path):
    _repo(tmp_path)
    m = build_repo_map(tmp_path, 1024)
    assert "class Engine  [spin]" in m
