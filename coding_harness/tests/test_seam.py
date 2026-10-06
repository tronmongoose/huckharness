"""Seam guard — keeps coding_harness extraction-ready (decided 2026-08-02).

coding_harness reaches into a wider deployment tree only through a small,
sanctioned set of adapter modules. Keeping that seam small is exactly what makes
the harness a lift-and-shift extraction when we split it into its own repo at the
P1->P2 boundary. This test fails if a NEW direct pipelines/ or agents/ import
appears anywhere in coding_harness outside the allowlist — so the seam can only
shrink, never quietly grow.
"""
from __future__ import annotations

import ast
from pathlib import Path

_HARNESS = Path(__file__).resolve().parents[1]  # coding_harness/
# The only modules permitted to import from a wider deployment tree.
_ALLOWED = {"core/router.py", "core/cost.py", "modes/ops.py"}
_FOREIGN = {"pipelines", "agents"}


def _foreign_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in _FOREIGN:
                hits.append(node.module)
        elif isinstance(node, ast.Import):
            hits.extend(
                a.name for a in node.names if a.name.split(".")[0] in _FOREIGN
            )
    return hits


def test_no_foreign_imports_outside_adapters() -> None:
    violations: dict[str, list[str]] = {}
    for path in _HARNESS.rglob("*.py"):
        rel = path.relative_to(_HARNESS).as_posix()
        if rel.startswith("tests/") or rel in _ALLOWED:
            continue
        hits = _foreign_imports(path)
        if hits:
            violations[rel] = hits
    assert not violations, (
        "coding_harness may reach a deployment only through the sanctioned "
        f"adapters {_ALLOWED}; new foreign imports found: {violations}"
    )
