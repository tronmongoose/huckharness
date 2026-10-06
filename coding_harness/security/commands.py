"""Shell command classifier for the autonomy ladder.

Exports ``ALLOW``, ``ASK``, ``DENY``, ``Decision``, ``LADDER``,
``LOW_MODULES``, ``classify``, ``segments`` and ``write_targets``.

``classify(cmd, level, settings)`` splits a command with ``shell_split`` into
its pipeline, list, subshell, process-substitution and line segments,
classifies each and returns the strictest decision (deny beats ask beats
allow). Per segment, in order: the built-in blocklists
from ``security.policy`` plus ``settings.commandBlocklist`` (deny),
``settings.commandDenylist`` (deny), a write redirection or ``git --output``
outside cwd (deny below HIGH), ``settings.commandAllowlist`` (allow from LOW
up), then the ladder tier the segment's leading words belong to. Anything the
ladder does not name at the current level is ``ask``; HIGH turns ask into
allow because it admits everything the blocklists leave alone. Any command
substitution (backticks, ``$(``, or ``${...(``) asks below HIGH on top of
whatever its segments decide, because quoting can hide the real word from
the tokenizer. ``python -m`` is LOW only for the test and lint modules in
``LOW_MODULES``; every other module is MEDIUM, and a script or ``-c`` is
never named by the ladder for a bare interpreter.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

from coding_harness.context import toolprobe
from coding_harness.core.mode import Autonomy
from coding_harness.core.settings import Settings
from coding_harness.security import policy, shell_split
from coding_harness.security.shell_split import segments, write_targets

__all__ = [
    "ALLOW", "ASK", "DENY", "Decision", "LADDER", "LOW_MODULES", "classify", "segments",
    "write_targets",
]

ALLOW = "allow"
ASK = "ask"
DENY = "deny"
_STRICTNESS = {ALLOW: 0, ASK: 1, DENY: 2}
_RANK = {Autonomy.OFF: 0, Autonomy.LOW: 1, Autonomy.MEDIUM: 2, Autonomy.HIGH: 3}

# Leading-word prefixes per lowest allowing level. The longest matching
# prefix wins, so ``make test`` (LOW) beats ``make`` (MEDIUM).
_TIERS: dict[Autonomy, tuple[str, ...]] = {
    Autonomy.OFF: ("ls", "cat", "head", "tail", "wc", "grep", "rg", "pwd", "echo", "which",
                   "git status", "git diff", "git log", "git show"),
    Autonomy.LOW: ("pytest", "ruff", "make test", "make lint", "npm test", "npm run test",
                   "go test", "cargo test", "git add"),
    Autonomy.MEDIUM: ("git commit", "npm install", "pip install", "cargo build", "go build",
                      "make"),
    Autonomy.HIGH: ("git push", "curl", "wget", "ssh"),
}
LADDER: tuple[tuple[tuple[str, ...], Autonomy], ...] = tuple(
    (tuple(prefix.split()), level)
    for level, prefixes in _TIERS.items()
    for prefix in prefixes
)
_FILE_OPS = frozenset({"mv", "cp", "mkdir", "touch", "chmod"})
_PYTHONS = frozenset({"python", "python3"})
_VENV_TOOLS = ("python", "ruff", "pytest", "mypy")
LOW_MODULES = frozenset({"pytest", "ruff", "py_compile", "unittest", "mypy", "flake8", "coverage"})
_LOW_MODULE_ARGS = frozenset({("black", "--check"), ("pip", "check")})
_SUBSTITUTION = re.compile(r"`|\$\(|\$\{[^}]*\(")
_FIND_MUTATORS = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})
_BRANCH_MUTATORS = frozenset({
    "-d", "-D", "--delete", "-m", "-M", "--move", "-c", "-C", "--copy",
    "-u", "--set-upstream-to", "--unset-upstream", "-f", "--force",
})
_DEV_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr"})
_HINTS = {
    Autonomy.OFF: "read-only commands only",
    Autonomy.LOW: "pytest, ruff, python -m <test or lint module> and the venv tool paths are allowed",
    Autonomy.MEDIUM: "commits, file ops inside cwd, installs and make are allowed",
}


@dataclass
class Decision:
    """Outcome for one command: ``action`` is allow, ask or deny."""

    action: str
    reason: str


def classify(
    command: str,
    level: Autonomy,
    settings: Settings | None = None,
    *,
    cwd: str | None = None,
) -> Decision:
    """Strictest decision over every segment of ``command`` at ``level``."""
    settings = settings or Settings()
    root = os.path.realpath(cwd or os.getcwd())
    decisions = [_segment(seg, level, settings, root) for seg in segments(command)]
    if _SUBSTITUTION.search(command):
        decisions.append(_ask(level, "command substitution"))
    if not decisions:
        return _ask(level, "empty command")
    return max(decisions, key=lambda d: _STRICTNESS[d.action])


def _segment(tokens: list[str], level: Autonomy, settings: Settings, root: str) -> Decision:
    """One segment: blocklists, write targets, allowlist, then the ladder tier."""
    words, _ = shell_split.strip_redirects(tokens)
    targets = shell_split.write_targets(tokens)
    blocked = _blocked(words, settings)
    if blocked is not None:
        return Decision(DENY, blocked)
    outside = [t for t in targets if t not in _DEV_SINKS and _escapes(t, root)]
    if outside and level is not Autonomy.HIGH:
        return Decision(DENY, f"redirect outside cwd: {outside[0]}")
    if level is Autonomy.OFF and any(t not in _DEV_SINKS for t in targets):
        return Decision(ASK, "write redirect at autonomy off")
    if not words:
        return Decision(ALLOW, "redirect only")
    entry = _prefixed(words, settings.command_allowlist)
    if entry is not None and level is not Autonomy.OFF:
        return Decision(ALLOW, f"allowlist: {entry}")
    tier = _tier(words, root)
    if tier is not None and _RANK[level] >= _RANK[tier]:
        return Decision(ALLOW, f"{tier.value} set")
    return _ask(level, f"'{_show(words)}' is not in the {level.value} autonomy set")


def _ask(level: Autonomy, reason: str) -> Decision:
    """ask below HIGH; HIGH admits whatever the blocklists did not refuse."""
    if level is Autonomy.HIGH:
        return Decision(ALLOW, "high set")
    hint = _HINTS.get(level)
    return Decision(ASK, f"{reason} ({hint})" if hint else reason)


def _blocked(words: list[str], settings: Settings) -> str | None:
    """Reason when the built-in or settings blocklists, or the denylist, match."""
    text = " ".join(words)
    for pattern, reason in policy.UNBYPASSABLE + policy.LEVEL_GATED:
        if re.search(pattern, text):
            return reason
    entry = _prefixed(words, settings.command_blocklist)
    if entry is not None:
        return f"blocklist: {entry}"
    entry = _prefixed(words, settings.command_denylist)
    if entry is not None:
        return f"denylist: {entry}"
    return None


def _prefixed(words: list[str], entries: list[str]) -> str | None:
    """First entry whose words are a leading run of ``words``."""
    for entry in entries:
        head = entry.split()
        if head and words[: len(head)] == head:
            return entry
    return None


def _tier(words: list[str], root: str) -> Autonomy | None:
    """Lowest level whose set names this segment; None when the ladder does not."""
    head = words[0]
    if head == "find":
        return None if set(words) & _FIND_MUTATORS else Autonomy.OFF
    if words[:2] == ["git", "branch"]:
        return None if _branch_mutates(words[2:]) else Autonomy.OFF
    if head == "cd":
        return Autonomy.OFF if len(words) == 2 and not _escapes(words[1], root) else None
    if head in _FILE_OPS:
        paths = [w for w in words[1:] if not w.startswith("-")]
        return None if any(_escapes(p, root) for p in paths) else Autonomy.MEDIUM
    venv = _venv_paths(root) if head.startswith("/") else {}
    if head in _PYTHONS or head == venv.get("python"):
        return _python_tier(words[1:], trusted=head not in _PYTHONS)
    if head in venv.values():
        return Autonomy.LOW
    matches = [(p, level) for p, level in LADDER if tuple(words[: len(p)]) == p]
    return max(matches, key=lambda m: len(m[0]))[1] if matches else None


def _python_tier(args: list[str], *, trusted: bool) -> Autonomy | None:
    """LOW for a test or lint module, MEDIUM for any other module or a venv-run script."""
    if args[:1] == ["-m"] and len(args) > 1:
        if args[1] in LOW_MODULES or tuple(args[1:3]) in _LOW_MODULE_ARGS:
            return Autonomy.LOW
        return Autonomy.MEDIUM
    if not args or args[0].startswith("-"):
        return None
    return Autonomy.MEDIUM if trusted else None


def _branch_mutates(args: list[str]) -> bool:
    """git branch with a positional name or a mutating flag creates or edits."""
    return any(a in _BRANCH_MUTATORS or not a.startswith("-") for a in args)


def _venv_paths(root: str) -> dict[str, str]:
    """Interpreter and tool paths the tool probe resolved for root, by tool name."""
    tools = toolprobe.probe(root).get("tools", {})
    return {name: tools[name]["path"] for name in _VENV_TOOLS if name in tools}


def _escapes(target: str, root: str) -> bool:
    """True unless ``target`` provably resolves inside ``root``."""
    if "$" in target or "`" in target:
        return True
    resolved = os.path.realpath(os.path.join(root, os.path.expanduser(target)))
    return resolved != root and not resolved.startswith(root + os.sep)


def _show(words: list[str]) -> str:
    """Leading words of a segment for a reason string."""
    return " ".join(words[:4]) + (" ..." if len(words) > 4 else "")
