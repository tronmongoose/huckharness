"""Deterministic repository map for the harness system prompt (P0-1).

Builds a symbol graph from the repo's Python files (stdlib ``ast`` — the repo is
Python-first, so no tree-sitter dependency), ranks files by idf-weighted
in-degree over who-references-whom, and renders the top definitions into a budget
injected before the first turn. Pure and local — no network, no model call — so
it passes the sensitivity gate trivially. The map orients the model to where
things live so it Greps/Reads less (the P0-1 pass-bar).

Scope is git-tracked files (respects .gitignore, excludes vendored venvs); a
bounded filesystem walk is the fallback when git is unavailable. Everything is
best-effort: any failure yields an empty map rather than blocking the session.

Exports: build_repo_map(root, budget_tokens).
"""
from __future__ import annotations

import ast
import subprocess
from pathlib import Path

_MAX_FILES = 2000          # bound the parse cost at session start
_PER_FILE_SYMBOLS = 8      # spread the budget across files, not one giant file
_CHARS_PER_TOKEN = 4       # rough budget conversion for the local model
_DEFAULT_BUDGET_TOKENS = 1024
_MAX_DEFINERS = 8          # a name defined in more files than this is generic noise
_IGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "build", "dist", "site-packages", ".tox", ".eggs",
}


def _is_test(path: Path) -> bool:
    """Test files reference symbols but rarely define orientation-worthy ones;
    keep them out of the map so the budget goes to real modules."""
    return (
        "tests" in path.parts
        or path.name.startswith("test_")
        or path.name.endswith("_test.py")
    )


def _source_files(root: Path) -> list[Path]:
    """Tracked non-test .py files (git), else a bounded, ignore-aware walk."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "*.py"], cwd=root, capture_output=True,
            text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            files = [root / line for line in out.stdout.splitlines() if line]
            files = [f for f in files if not _is_test(f)]
            return files[:_MAX_FILES]
    except (OSError, subprocess.SubprocessError):
        pass
    files = []
    for p in root.rglob("*.py"):
        if any(part in _IGNORE_DIRS for part in p.parts) or _is_test(p):
            continue
        files.append(p)
        if len(files) >= _MAX_FILES:
            break
    return files


def _signature(node: ast.AST) -> str:
    """A compact def/class signature for one AST node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        a = node.args
        parts = [arg.arg for arg in (*a.posonlyargs, *a.args)]
        if a.vararg:
            parts.append("*" + a.vararg.arg)
        if a.kwonlyargs:
            parts.extend(kw.arg for kw in a.kwonlyargs)
        if a.kwarg:
            parts.append("**" + a.kwarg.arg)
        return f"def {node.name}({', '.join(parts)})"
    methods = [
        n.name for n in node.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not n.name.startswith("_")
    ][:6]
    tail = f"  [{', '.join(methods)}]" if methods else ""
    return f"class {node.name}{tail}"


def _parse(path: Path) -> tuple[set[str], set[str], list[str]] | None:
    """Return (defined names, referenced names, rendered signatures) or None."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    defined: set[str] = set()
    sigs: list[str] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            sigs.append(_signature(node))
    referenced: set[str] = set()
    for n in ast.walk(tree):  # one pass: Name loads + attribute names
        if isinstance(n, ast.Name):
            referenced.add(n.id)
        elif isinstance(n, ast.Attribute):
            referenced.add(n.attr)
    return defined, referenced, sigs[:_PER_FILE_SYMBOLS]


def _rank(parsed: dict[Path, tuple[set[str], set[str], list[str]]]) -> dict[Path, float]:
    """Score each file by idf-weighted in-degree: how much of the rest of the
    repo references the symbols it defines. Weighted in-degree (not PageRank)
    on purpose — PageRank lets a densely self-referential subsystem inflate its
    own rank, which buries the broadly-depended-upon infrastructure files."""
    definers: dict[str, list[Path]] = {}
    for path, (defined, _ref, _sigs) in parsed.items():
        for name in defined:
            definers.setdefault(name, []).append(path)
    # idf weight: a distinctive name (defined once) carries full signal; a
    # generic one (get/load/run, defined everywhere) is dropped as noise.
    weight = {
        name: 1.0 / len(files)
        for name, files in definers.items()
        if len(name) >= 3 and not name.startswith("__") and len(files) <= _MAX_DEFINERS
    }
    score: dict[Path, float] = dict.fromkeys(parsed, 0.0)
    for path, (_defined, referenced, _sigs) in parsed.items():
        for name in referenced:
            w = weight.get(name)
            if not w:
                continue
            for target in definers[name]:
                if target != path:
                    score[target] += w
    return score


def build_repo_map(root: str | Path, budget_tokens: int = _DEFAULT_BUDGET_TOKENS) -> str:
    """Ranked symbol map for ``root``, capped at ``budget_tokens``. Empty on any
    failure or when the repo has no parseable Python."""
    try:
        base = Path(root).resolve()
        parsed = {}
        for path in _source_files(base):
            info = _parse(path)
            if info and info[2]:  # has renderable signatures
                parsed[path] = info
        if not parsed:
            return ""
        scores = _rank(parsed)
        ranked = sorted(parsed, key=lambda p: (-scores.get(p, 0.0), str(p)))
        budget = budget_tokens * _CHARS_PER_TOKEN
        lines: list[str] = []
        used = 0
        for path in ranked:
            rel = path.relative_to(base) if path.is_relative_to(base) else path
            block = [str(rel)] + [f"  {s}" for s in parsed[path][2]]
            chunk = "\n".join(block)
            if used + len(chunk) > budget:
                break
            lines.append(chunk)
            used += len(chunk) + 1
        if not lines:
            return ""
        return (
            "\n\n## Repository map (top symbols by reference rank)\n\n"
            + "\n".join(lines) + "\n"
        )
    except Exception:  # noqa: BLE001 — a map is a nicety; never break a session
        return ""
