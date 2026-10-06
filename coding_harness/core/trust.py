"""Per-project trust: which directories may run their own hooks, settings and MCP servers.

Exports ``TRUST_FILE``, ``trusted_roots``, ``is_trusted`` and ``trust``.

A cloned repository is untrusted input. Its ``.bjorn/settings.json`` can name
hooks and raise autonomy, its ``.mcp.json`` spawns processes and its
``.claude/hooks/sentinel-gate.py`` is executed on every tool call, so none of
those take effect until the user has trusted the directory once with
``bjorn trust``. A trusted directory covers everything beneath it.
``HARNESS_TRUST_PROJECTS=all`` trusts every directory, for deployments and CI
that only ever run in their own checkouts.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

TRUST_FILE = Path("~/.config/bjorn/trusted_projects.json")


def _trust_path() -> Path:
    """The trust file with ``~`` expanded."""
    return Path(os.path.expanduser(str(TRUST_FILE)))


def trusted_roots() -> list[Path]:
    """Directories the user has trusted; empty when the file is absent or unreadable."""
    try:
        data = json.loads(_trust_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    return [Path(p) for p in data if isinstance(p, str) and os.path.isabs(p)]


def is_trusted(directory: Path) -> bool:
    """True when ``directory`` is a trusted root or lies beneath one."""
    if os.environ.get("HARNESS_TRUST_PROJECTS") == "all":
        return True
    target = Path(os.path.realpath(str(directory)))
    return any(root == target or root in target.parents for root in trusted_roots())


def trust(directory: Path) -> Path:
    """Add ``directory`` to the trust file, written atomically at 0600; returns the stored root."""
    root = Path(os.path.realpath(str(directory)))
    roots = trusted_roots()
    if root not in roots:
        roots.append(root)
    path = _trust_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".trust-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(sorted(str(r) for r in roots), indent=2) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return root
