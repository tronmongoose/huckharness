"""Custom slash commands: /<name> expands a markdown prompt template.

Exports ``COMMANDS_USER``, ``COMMANDS_PROJECT_REL`` and ``expand``.
``expand("/foo bar baz")`` reads ``<git root>/.bjorn/commands/foo.md``
(project wins) or ``~/.config/bjorn/commands/foo.md`` and returns its text
with ``$ARGUMENTS`` replaced by ``"bar baz"``; None when the line is not a
custom command, so built-in slash commands are unaffected.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from coding_harness.core.settings import _project_root

COMMANDS_USER = Path("~/.config/bjorn/commands")
COMMANDS_PROJECT_REL = Path(".bjorn") / "commands"
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def _resolve(name: str, cwd: str) -> Path | None:
    """The command file for ``name``: project first, then user."""
    candidates = (
        _project_root(cwd) / COMMANDS_PROJECT_REL / f"{name}.md",
        Path(os.path.expanduser(str(COMMANDS_USER))) / f"{name}.md",
    )
    for path in candidates:
        if path.is_file():
            return path
    return None


def expand(line: str, cwd: str | None = None) -> str | None:
    """``/<name> args`` as the template text with $ARGUMENTS substituted."""
    line = line.strip()
    if not line.startswith("/"):
        return None
    name, _, args = line[1:].partition(" ")
    if not _NAME_RE.match(name):
        return None
    path = _resolve(name, cwd or os.getcwd())
    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return text.replace("$ARGUMENTS", args.strip())
