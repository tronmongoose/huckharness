"""Per-suffix syntax verifiers for the in-loop verify gate (P1-4).

Exports: Check, verify_paths.

Each edited file is checked by the cheapest tool that can say whether it
parses: py_compile, bash -n, json.loads, tomllib, yaml.safe_load, node
--check, tsc --noEmit, gofmt + go vet, cargo check, make -n. A check whose
tool is absent is a SKIP with a reason, never a silent pass, so the session
log shows exactly which edits went unverified. Nothing here routes through
the tool registry: these are the harness's own commands, not the model's.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_TIMEOUT_S = 120
_MAX_DETAIL_CHARS = 1500
_MAKEFILE_NAMES = ("Makefile", "GNUmakefile", "makefile")


@dataclass
class Check:
    """One verifier outcome; detail carries the tool output or the SKIP reason."""

    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


def _run(name: str, cmd: list[str], cwd: Path) -> Check:
    """Run one tool command: nonzero exit is FAIL, a timeout is a SKIP with a reason."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, cwd=str(cwd), timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return Check(name, "SKIP", f"timed out after {_TIMEOUT_S}s")
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()[-_MAX_DETAIL_CHARS:]
    return Check(name, "PASS" if proc.returncode == 0 else "FAIL", out)


def _tool(name: str, binary: str, cmd: list[str], cwd: Path) -> Check:
    """Run cmd when its binary is on PATH, else SKIP naming the missing tool."""
    if shutil.which(binary) is None:
        return Check(name, "SKIP", f"{binary} not on PATH")
    return _run(name, cmd, cwd)


def _loads(name: str, path: Path, loader: Callable[[str], object]) -> Check:
    """Parse a data file in-process; the loader's exception text is the FAIL detail."""
    try:
        loader(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001 - each loader raises its own parse error type
        return Check(name, "FAIL", f"{type(e).__name__}: {e}"[:_MAX_DETAIL_CHARS])
    return Check(name, "PASS")


def _nearest(path: Path, marker: str) -> Path | None:
    """The closest ancestor of path holding marker, or None."""
    for parent in path.parents:
        if (parent / marker).is_file():
            return parent
    return None


def _per_root(
    paths: list[Path], marker: str, label: str, binary: str,
    cmd: Callable[[Path], list[str]],
) -> list[Check]:
    """One package-level check per nearest marker root; a file with no root is a SKIP."""
    roots = {_nearest(p, marker) for p in paths}
    checks: list[Check] = []
    if None in roots:
        checks.append(Check(label, "SKIP", f"no {marker} above an edited file"))
    for root in sorted(r for r in roots if r is not None):
        checks.append(_tool(f"{label} in {root}", binary, cmd(root), root))
    return checks


def _py(paths: list[Path], cwd: Path) -> list[Check]:
    """Byte-compile each file."""
    return [
        _run(f"py_compile {p}", [sys.executable, "-m", "py_compile", str(p)], cwd)
        for p in paths
    ]


def _sh(paths: list[Path], cwd: Path) -> list[Check]:
    """bash -n per script."""
    return [_tool(f"bash -n {p}", "bash", ["bash", "-n", str(p)], cwd) for p in paths]


def _json(paths: list[Path], cwd: Path) -> list[Check]:
    """json.loads per file."""
    return [_loads(f"json {p}", p, json.loads) for p in paths]


def _toml(paths: list[Path], cwd: Path) -> list[Check]:
    """tomllib per file on 3.11+, else SKIP."""
    if sys.version_info < (3, 11):
        return [Check(f"toml {p}", "SKIP", "needs python 3.11") for p in paths]
    import tomllib
    return [_loads(f"toml {p}", p, tomllib.loads) for p in paths]


def _yaml(paths: list[Path], cwd: Path) -> list[Check]:
    """yaml.safe_load per file when pyyaml imports, else SKIP."""
    try:
        import yaml
    except ImportError:
        return [Check(f"yaml {p}", "SKIP", "pyyaml not importable") for p in paths]
    return [_loads(f"yaml {p}", p, yaml.safe_load) for p in paths]


def _js(paths: list[Path], cwd: Path) -> list[Check]:
    """node --check per file."""
    return [
        _tool(f"node --check {p}", "node", ["node", "--check", str(p)], cwd) for p in paths
    ]


def _ts(paths: list[Path], cwd: Path) -> list[Check]:
    """tsc --noEmit against the nearest tsconfig.json."""
    return _per_root(
        paths, "tsconfig.json", "tsc --noEmit", "tsc",
        lambda root: ["tsc", "--noEmit", "-p", str(root / "tsconfig.json")],
    )


def _gofmt(path: Path, cwd: Path) -> Check:
    """gofmt -l: a nonzero exit is a parse error, a listed file needs formatting."""
    check = _tool(f"gofmt -l {path}", "gofmt", ["gofmt", "-l", str(path)], cwd)
    if check.status == "PASS" and check.detail:
        return Check(check.name, "FAIL", f"not gofmt-formatted: {check.detail}")
    return check


def _go(paths: list[Path], cwd: Path) -> list[Check]:
    """gofmt per file plus go vet once per module."""
    checks = [_gofmt(p, cwd) for p in paths]
    return checks + _per_root(
        paths, "go.mod", "go vet ./...", "go", lambda _root: ["go", "vet", "./..."],
    )


def _rs(paths: list[Path], cwd: Path) -> list[Check]:
    """cargo check once per crate."""
    return _per_root(
        paths, "Cargo.toml", "cargo check", "cargo", lambda _root: ["cargo", "check"],
    )


def _make(paths: list[Path], cwd: Path) -> list[Check]:
    """make -n dry run per Makefile."""
    return [
        _tool(f"make -n {p}", "make", ["make", "-n", "-f", str(p)], p.parent) for p in paths
    ]


_BY_SUFFIX: dict[str, Callable[[list[Path], Path], list[Check]]] = {
    ".py": _py, ".sh": _sh, ".json": _json, ".toml": _toml, ".yaml": _yaml,
    ".yml": _yaml, ".js": _js, ".mjs": _js, ".ts": _ts, ".go": _go, ".rs": _rs,
}


def _kind(path: Path) -> str:
    """The dispatch key for a path: its suffix, or 'Makefile' by name."""
    if path.name in _MAKEFILE_NAMES:
        return "Makefile"
    return path.suffix.lower()


def verify_paths(paths: list[str], cwd: str | None = None) -> list[Check]:
    """Verify every existing path by suffix; a suffix with no verifier is a SKIP."""
    base = Path(cwd or os.getcwd())
    groups: dict[str, list[Path]] = {}
    for raw in sorted(set(paths)):
        path = Path(raw)
        if path.is_file():
            groups.setdefault(_kind(path), []).append(path)
    checks: list[Check] = []
    for kind, group in groups.items():
        handler = _make if kind == "Makefile" else _BY_SUFFIX.get(kind)
        if handler is None:
            checks += [Check(f"verify {p}", "SKIP", f"no verifier for {kind or p.name}")
                       for p in group]
            continue
        checks += handler(group, base)
    return checks
