"""Every HARNESS_* environment variable the package reads is documented in the root README."""
from __future__ import annotations

import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
README = PACKAGE.parent / "README.md"
# Word boundary so a module constant like _HARNESS_INVOCATION_RE is not an env var.
NAME_RE = re.compile(r"\bHARNESS_[A-Z0-9_]+")
EXTRA_NAMES = ("OLLAMA_URL", "SENTINEL_GATE_HOOK")


def _names_in_package() -> set[str]:
    """HARNESS_* names used anywhere in the package outside tests."""
    found: set[str] = set()
    for path in PACKAGE.rglob("*.py"):
        if "tests" in path.relative_to(PACKAGE).parts:
            continue
        found.update(NAME_RE.findall(path.read_text(encoding="utf-8")))
    return found


def test_every_harness_env_var_is_in_readme() -> None:
    """The README env-var table names every HARNESS_* the code reads."""
    names = _names_in_package()
    assert names, "grep found no HARNESS_* names; the scan is broken"
    readme = README.read_text(encoding="utf-8")
    missing = sorted(n for n in (*names, *EXTRA_NAMES) if f"`{n}`" not in readme)
    assert not missing, f"undocumented in README.md: {missing}"
