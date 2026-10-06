"""In-process Sentinel policy: the fail-closed gate every tool call passes.

Exports ``POLICY_VERSION``, ``SentinelVerdict``, ``CATEGORIES``,
``UNBYPASSABLE``, ``LEVEL_GATED``, ``DENY_WRITE``, the ``PATH_*`` verdict
labels and ``review``.

``review`` decides in a fixed order: the unbypassable blocklist (no setting can
remove an entry), the level-gated list (phase 0 denies everywhere; a later
phase may relax it at a higher trust level), the tool-category rule, the
autonomy ladder from ``security.commands`` for execute calls when a ``level``
is given (deny, or an ``ask`` verdict the registry may broker), then the
optional deployment hook from ``security.hook_adapter``. One blocklist rule is a
parser rather than a regex: every recursive ``rm`` target is resolved against
``cwd`` and any target that escapes it is blocklisted, while a recursive rm
that stays inside cwd is only level-gated. Writes (Write, Edit, and every
Bash redirect or ``git --output`` target) to the ``DENY_WRITE`` roots, to
``.git``, to ``<cwd>/.claude`` or ``<cwd>/.bjorn``, or onto the active
Sentinel hook are blocklisted so the gate cannot be edited from inside.
"""
from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from coding_harness.security import hook_adapter

if TYPE_CHECKING:
    from coding_harness.core.mode import Autonomy
    from coding_harness.core.settings import Settings

POLICY_VERSION = "1"
CATEGORIES = ("read", "edit", "execute")

PATH_BLOCKLIST = f"policy:v{POLICY_VERSION}:blocklist"
PATH_LEVEL_GATED = f"policy:v{POLICY_VERSION}:level_gated"
PATH_UNKNOWN_CATEGORY = f"policy:v{POLICY_VERSION}:unknown_category"
PATH_AUTONOMY = f"policy:v{POLICY_VERSION}:autonomy"
PATH_ASK = f"policy:v{POLICY_VERSION}:ask"
PATH_HOOK = f"policy:v{POLICY_VERSION}:hook"
PATH_CATEGORY = f"policy:v{POLICY_VERSION}:category"

UNBYPASSABLE: tuple[tuple[str, str], ...] = (
    (r">\s*/dev/(?:sd[a-z]|disk\d*)", "raw disk write"),
    (r"\bmkfs(?:\.\w+)?\b", "mkfs (format)"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "dd to a block device"),
    (r":\(\)\s*\{.*\|.*&\s*\}\s*;", "fork bomb"),
    (r"\bsudo\b", "sudo"),
    (r"\b(?:curl|wget)\b[^|]*\|\s*(?:ba|z)?sh\b", "remote script piped to a shell"),
    (
        r"\bgit\s+push\b[^|;&]*\s(?:--force\b|-[a-zA-Z]*f[a-zA-Z]*(?=\s|$))",
        "git push --force",
    ),
)

LEVEL_GATED: tuple[tuple[str, str], ...] = (
    (
        r"\brm\s+(?:-\S+\s+)*(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(?=\s|$)",
        "recursive rm",
    ),
    (r"\bgit\s+clean\b[^|;&]*\s(?:-[a-zA-Z]*f[a-zA-Z]*|--force)(?=\s|$)", "git clean -f"),
    (r"\bgit\s+reset\b[^|;&]*\s--hard\b", "git reset --hard"),
    (r"\bgit\s+(?:checkout|restore)\b[^|;&]*\s\./?(?=\s|$|[;&|])", "working-tree reset"),
)

DENY_WRITE: tuple[str, ...] = (
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gh",
    "~/Library/Keychains",
)

_CWD_PROTECTED: tuple[str, ...] = (".claude", ".bjorn")

_SEPARATORS = frozenset({";", ";;", "&&", "||", "|", "|&", "&", "(", ")"})


@dataclass
class SentinelVerdict:
    """Outcome of one review; ``path`` names the rule that decided it."""

    allowed: bool
    reason: str
    path: str


def review(
    tool_name: str,
    category: str | None,
    tool_input: dict[str, Any],
    session_id: str,
    *,
    cwd: str,
    level: Autonomy | None = None,
    settings: Settings | None = None,
) -> SentinelVerdict:
    """Decide one tool call: blocklist, level gate, category, ladder, then hook."""
    reason = _blocklisted(tool_name, tool_input, cwd)
    if reason is not None:
        return SentinelVerdict(False, reason, PATH_BLOCKLIST)
    reason = _level_gated(tool_name, tool_input)
    if reason is not None:
        return SentinelVerdict(False, reason, PATH_LEVEL_GATED)
    if category not in CATEGORIES:
        return SentinelVerdict(
            False,
            f"unknown tool category {category!r} for {tool_name}",
            PATH_UNKNOWN_CATEGORY,
        )
    if level is not None and category == "execute":
        ladder = _ladder(tool_input, level, settings, cwd)
        if ladder is not None:
            return ladder
    hook = hook_adapter.review(tool_name, category, tool_input, session_id, cwd=cwd)
    if hook is not None:
        return SentinelVerdict(hook.allowed, hook.reason, PATH_HOOK)
    return SentinelVerdict(True, f"{category} tool allowed", PATH_CATEGORY)


def _ladder(
    tool_input: dict[str, Any], level: Autonomy, settings: Settings | None, cwd: str,
) -> SentinelVerdict | None:
    """Autonomy verdict for a shell command; None when the ladder allows it."""
    from coding_harness.security import commands

    command = _command(tool_input)
    if not command:
        return None
    decision = commands.classify(command, level, settings, cwd=cwd)
    if decision.action == commands.DENY:
        return SentinelVerdict(False, f"autonomy_denied:{decision.reason}", PATH_AUTONOMY)
    if decision.action == commands.ASK:
        return SentinelVerdict(False, f"autonomy_ask:{decision.reason}", PATH_ASK)
    return None


def _blocklisted(tool_name: str, tool_input: dict[str, Any], cwd: str) -> str | None:
    """Reason when the call hits a rule no setting can remove."""
    if tool_name == "Bash":
        command = _command(tool_input)
        for pattern, reason in UNBYPASSABLE:
            if re.search(pattern, command):
                return reason
        target = _rm_escape(command, cwd)
        if target is not None:
            return f"recursive rm outside cwd: {target}"
        for target in _bash_write_targets(command):
            reason = _write_target_blocked(target, cwd)
            if reason is not None:
                return reason
        return None
    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path")
        if isinstance(file_path, str) and file_path:
            return _write_target_blocked(file_path, cwd)
    return None


def _level_gated(tool_name: str, tool_input: dict[str, Any]) -> str | None:
    """Reason when the call hits a rule phase 0 denies at every trust level."""
    if tool_name != "Bash":
        return None
    command = _command(tool_input)
    for pattern, reason in LEVEL_GATED:
        if re.search(pattern, command):
            return reason
    return None


def _command(tool_input: dict[str, Any]) -> str:
    """The Bash command text, or empty when absent or not a string."""
    command = tool_input.get("command")
    return command if isinstance(command, str) else ""


def _bash_write_targets(command: str) -> list[str]:
    """Every redirect or ``git --output`` path a Bash command writes to."""
    from coding_harness.security import commands

    return [t for seg in commands.segments(command) for t in commands.write_targets(seg)]


def _write_target_blocked(file_path: str, cwd: str) -> str | None:
    """Reason when a write target lands in a protected location."""
    raw = os.path.normpath(os.path.join(cwd, os.path.expanduser(file_path)))
    candidates = (raw, os.path.realpath(raw))
    roots = [(root, os.path.expanduser(root)) for root in DENY_WRITE]
    roots += [(rel, os.path.join(cwd, rel)) for rel in _CWD_PROTECTED]
    for label, expanded in roots:
        for prefix in (expanded, os.path.realpath(expanded)):
            if any(_under(c, prefix) for c in candidates):
                return f"write under protected path {label}"
    hook = hook_adapter.resolve(cwd)
    if hook is not None and os.path.realpath(hook) in candidates:
        return "write onto the sentinel hook"
    if any("/.git/" in c for c in candidates):
        return "write inside .git"
    return None


def _under(candidate: str, prefix: str) -> bool:
    """Whether ``candidate`` is ``prefix`` or lies beneath it."""
    return candidate == prefix or candidate.startswith(prefix + os.sep)


def _rm_escape(command: str, cwd: str) -> str | None:
    """First recursive-rm target that resolves outside ``cwd``, if any."""
    root = os.path.realpath(cwd)
    for args in _rm_invocations(_tokens(command)):
        if not _is_recursive(args):
            continue
        for target in _targets(args):
            if _escapes(target, root):
                return target
    return None


def _tokens(command: str) -> list[str]:
    """Shell-split with separators as their own tokens; raw split on bad quoting."""
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        return list(lex)
    except ValueError:
        return command.split()


def _rm_invocations(tokens: list[str]) -> Iterator[list[str]]:
    """Argument list of every ``rm`` in the token stream."""
    i = 0
    n = len(tokens)
    while i < n:
        if tokens[i] != "rm" and not tokens[i].endswith("/rm"):
            i += 1
            continue
        args: list[str] = []
        i += 1
        while i < n and tokens[i] not in _SEPARATORS:
            args.append(tokens[i])
            i += 1
        yield args


def _is_recursive(args: list[str]) -> bool:
    """Whether an rm argument list carries -r / -R / --recursive."""
    for arg in args:
        if arg == "--":
            return False
        if arg == "--recursive":
            return True
        if arg.startswith("-") and not arg.startswith("--") and ("r" in arg or "R" in arg):
            return True
    return False


def _targets(args: list[str]) -> list[str]:
    """Positional rm arguments, honoring ``--`` as end of options."""
    targets: list[str] = []
    options_done = False
    for arg in args:
        if not options_done and arg == "--":
            options_done = True
            continue
        if options_done or not arg.startswith("-"):
            targets.append(arg)
    return targets


def _escapes(target: str, root: str) -> bool:
    """True unless ``target`` provably resolves inside ``root``."""
    if "$" in target or "`" in target:
        return True
    resolved = os.path.realpath(os.path.join(root, os.path.expanduser(target)))
    return resolved != root and not resolved.startswith(root + os.sep)
