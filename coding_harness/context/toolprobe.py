"""Tool probe for the harness system prompt (P0-3).

Resolves the exact interpreter and CLI paths the model should use for a working
directory so a local model stops guessing at ``ruff`` on PATH or inventing
flags. The venv under cwd (or the git root) wins over PATH. Byte-stable per cwd
(no timestamps), cached per cwd, never raises: a tool whose ``--version`` fails
or hangs renders as not available. Kill switch: HARNESS_TOOL_PROBE=0.

Exports: probe(cwd), render(info).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

_TOOLS = ("ruff", "pytest", "make", "node", "git")
_VERSION_TIMEOUT = 2.0
_MAX_WALK_DEPTH = 25
_PY_VERSION_CMD = "import sys; print(sys.version.split()[0])"
_CACHE: dict[str, dict] = {}  # GLOBAL-STATE: per-cwd probe cache; each probe spawns subprocesses


def _git_root(cwd: Path) -> Path | None:
    """Nearest ancestor (or cwd) holding a .git entry, bounded walk."""
    cur = cwd
    for _ in range(_MAX_WALK_DEPTH):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


def _venv_bin(cwd: Path) -> Path | None:
    """<cwd or git root>/.venv/bin when it exists."""
    for base in (cwd, _git_root(cwd)):
        if base is not None and (base / ".venv" / "bin").is_dir():
            return base / ".venv" / "bin"
    return None


def _first_line(argv: list[str]) -> str | None:
    """First output line of argv, or None when it fails, hangs, or prints nothing."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_VERSION_TIMEOUT)
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    lines = (proc.stdout or proc.stderr).strip().splitlines()
    return lines[0].strip() if lines else ""


def _find(name: str, venv_bin: Path | None) -> str | None:
    """Executable path for name: venv bin dir first, then PATH."""
    if venv_bin is not None and os.access(venv_bin / name, os.X_OK):
        return str(venv_bin / name)
    return shutil.which(name)


def _probe_python(venv_bin: Path | None) -> tuple[str, str | None] | None:
    """Interpreter path plus bare version, venv first, else sys.executable."""
    path = str(venv_bin / "python") if venv_bin is not None else sys.executable
    if venv_bin is not None and not os.access(path, os.X_OK):
        path = sys.executable
    version = _first_line([path, "-c", _PY_VERSION_CMD])
    return None if version is None else (path, version or None)


def _probe_tool(name: str, venv_bin: Path | None) -> tuple[str, str | None] | None:
    """Path plus first --version line for name, or None when unusable."""
    path = _find(name, venv_bin)
    if path is None:
        return None
    version = _first_line([path, "--version"])
    return None if version is None else (path, version or None)


def probe(cwd: str) -> dict:
    """Resolve python and common CLI tools for cwd; cached, never raises."""
    if os.environ.get("HARNESS_TOOL_PROBE", "1") == "0":
        return {}
    key = str(Path(cwd).resolve())
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    info: dict = {"cwd": key, "tools": {}, "missing": []}
    try:
        venv_bin = _venv_bin(Path(key))
        found = {"python": _probe_python(venv_bin)}
        found.update((name, _probe_tool(name, venv_bin)) for name in _TOOLS)
    except Exception:  # noqa: BLE001
        found = dict.fromkeys(("python", *_TOOLS))
    for name, hit in found.items():
        if hit is None:
            info["missing"].append(name)
        else:
            info["tools"][name] = {"path": hit[0], "version": hit[1]}
    _CACHE[key] = info
    return info


def render(info: dict) -> str:
    """Injectable tooling block for the system prompt; '' when disabled or empty."""
    if os.environ.get("HARNESS_TOOL_PROBE", "1") == "0" or not info:
        return ""
    lines = ["Available tooling (use these exact paths):"]
    for name, entry in info.get("tools", {}).items():
        suffix = f" ({entry['version']})" if entry.get("version") else ""
        lines.append(f"  {name}: {entry['path']}{suffix}")
    missing = info.get("missing") or []
    if missing:
        lines.append(f"  not available: {', '.join(missing)}")
    return "\n\n" + "\n".join(lines) + "\n"
