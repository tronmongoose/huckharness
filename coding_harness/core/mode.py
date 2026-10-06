"""Plan vs Act mode for the coding harness (sl-yli.2 + sl-yli.4).

Plan mode = read-only investigation. The model only sees tools that cannot
mutate the filesystem or invoke the shell, so it physically cannot perform
a write even if the user asks for one. The Sentinel hook stays in place,
but the *visibility* gate runs before Sentinel — what the model can't see,
it can't request.

Act mode = full surface (the original harness behavior). Every built-in
plus every MCP tool, all gated by Sentinel + operator confirm.

Classification surfaces:

  * ``BUILTIN_PLAN_SAFE`` — frozen set of harness built-ins safe in Plan.
  * ``MCP_PLAN_SAFE_NAMES`` — module-level set of literal MCP tool names
    known to be read-only.
  * ``MCP_PLAN_SAFE_PATTERNS`` — fnmatch globs (e.g. ``gmail_search``,
    ``drive_get_*``). Patterns let a new MCP server contribute "anything
    matching this prefix is read-only" without listing every tool.
  * ``register_mcp_plan_safe(...)`` — runtime hook used by
    ``ToolRegistry.register_mcp_server`` to merge per-server plan-safe
    declarations from ``.mcp.json`` into the live classifier.

Extending this is intentional and not a "trust the server" affair. A new
MCP server's plan-safe tools must be enumerated either by literal name
(sl-yli.4 does this for Gmail/Calendar/Drive read tools) or by glob
pattern in ``.mcp.json``. Anything we haven't heard of fails closed to
Act-only — Plan mode's safety property is enforced by enumeration.
"""
from __future__ import annotations

import fnmatch
import threading
from enum import Enum

# ── Built-in tools ──────────────────────────────────────────────────

# Plan-safe built-ins. Read/Grep/Glob inspect disk without mutating it
# and never spawn subprocesses, so they're safe to expose to the model
# during investigation work.
BUILTIN_PLAN_SAFE: frozenset[str] = frozenset({
    "Read",
    "Grep",
    "Glob",
    # Brain talks to a long-lived index process but only reads from it, and
    # its tier cap applies in Plan as in Act.
    "Brain",
    # TodoWrite keeps a checklist in memory and mirrors it under meta_dir();
    # it touches no project file, so a planning turn may draft the steps.
    "TodoWrite",
    # Explore runs a child session whose only tools are Read, Grep and Glob.
    "Explore",
})

# Built-ins explicitly act-only — listed for clarity even though
# anything not in BUILTIN_PLAN_SAFE is treated as act-only.
BUILTIN_ACT_ONLY: frozenset[str] = frozenset({
    "Bash",
    "Write",
    "Edit",
})

# ── MCP tools ───────────────────────────────────────────────────────

# Specific MCP tool names known to be plan-safe. Module-mutable (not a
# frozenset) so ``register_mcp_plan_safe`` can extend it at runtime when
# a server's per-config declarations come in from ``.mcp.json``.
MCP_PLAN_SAFE_NAMES: set[str] = {
    # Carryall read-class tools. This set is also what gives
    # an MCPTool its "read" category for the in-process Sentinel policy.
    "carryall_list_vaults",
    "carryall_get_metadata",
    "carryall_check_access",
    "carryall_query_documents",
    "carryall_audit_log",
    "carryall_read_document",

    # Gmail (sl-yli.4 — pre-classified for future OAuth-DCR wiring).
    # Anthropic-hosted; harness can't reach them yet, but classification
    # is in place so when remote MCP transport lands these are plan-safe
    # out of the box.
    "gmail_search",
    "gmail_read_message",
    "gmail_list_labels",
    "gmail_list_messages",
    "gmail_list_drafts",
    "gmail_get_thread",
    "gmail_get_message",

    # Google Calendar (sl-yli.4 — pre-classified)
    "calendar_list_events",
    "calendar_list_calendars",
    "calendar_get_event",
    "calendar_suggest_time",

    # Google Drive (sl-yli.4 — pre-classified)
    "drive_search_files",
    "drive_list_recent_files",
    "drive_get_file_metadata",
    "drive_get_file_permissions",
    "drive_read_file_content",
    "drive_download_file_content",
}

# Glob patterns matched with ``fnmatch.fnmatchcase``. Per-server
# ``plan_safe_patterns`` from ``.mcp.json`` get merged here at
# registration time. Use sparingly — broad matches like ``*_search``
# can sweep up write-class tools whose names happen to share a suffix.
MCP_PLAN_SAFE_PATTERNS: list[str] = []

_lock = threading.Lock()


class Mode(str, Enum):
    """Operating mode for the harness.

    Subclassing ``str`` so the value serializes naturally into JSON audit
    events without callers needing ``.value``.
    """

    PLAN = "plan"
    ACT = "act"


class Autonomy(str, Enum):
    """Autonomy ladder: how much a session may do without asking.

    OFF is read-only (Plan mode). LOW adds edits under cwd plus the test,
    lint and build-check commands. MEDIUM adds commits, file moves and
    installs. HIGH adds network and everything the blocklist leaves alone.
    The shell command tiers live in ``security.commands``.
    """

    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def mode_for(level: Autonomy) -> Mode:
    """Tool-surface mode for a level: OFF is Plan, everything else is Act."""
    return Mode.PLAN if level is Autonomy.OFF else Mode.ACT


def parse_autonomy(text: str) -> Autonomy:
    """Case-insensitive level name to ``Autonomy``; ValueError on anything else."""
    try:
        return Autonomy(text.strip().lower())
    except ValueError:
        names = ", ".join(a.value for a in Autonomy)
        raise ValueError(f"unknown autonomy level {text!r}; expected one of {names}") from None


def set_autonomy(session, level: Autonomy, reason: str) -> Autonomy:
    """Move ``session`` to ``level``: registry level, tool surface, audit row.

    Shared by the REPL (/autonomy, /plan, /act) and serve. A no-op at the
    current level. Returns the previous level. The envelope is the caller's
    business: serve rebuilds its preset grants, the REPL has no envelope.
    """
    from coding_harness.security import audit

    previous = session.registry.autonomy
    if level is previous:
        return previous
    session.registry.autonomy = level
    session.set_mode(mode_for(level), reason=reason)
    audit.append_autonomy_change(
        session_id=session.session_id,
        from_level=previous.value,
        to_level=level.value,
        reason=reason,
        agent_identity=session.agent_identity,
    )
    return previous


def is_plan_safe(tool_name: str) -> bool:
    """Return True iff ``tool_name`` may run in Plan mode.

    Unknown tools are treated as act-only (fail-closed). The classifier
    consults, in order:

      1. ``BUILTIN_PLAN_SAFE`` literal names.
      2. ``MCP_PLAN_SAFE_NAMES`` literal names.
      3. ``MCP_PLAN_SAFE_PATTERNS`` glob patterns.

    Nothing else is plan-safe. Adding a new plan-safe tool requires an
    explicit update via ``register_mcp_plan_safe`` or by editing this
    module — Plan mode's safety property is enforced by enumeration,
    not by trust in the server.
    """
    if tool_name in BUILTIN_PLAN_SAFE:
        return True
    with _lock:
        if tool_name in MCP_PLAN_SAFE_NAMES:
            return True
        for pattern in MCP_PLAN_SAFE_PATTERNS:
            if fnmatch.fnmatchcase(tool_name, pattern):
                return True
    return False


def register_mcp_plan_safe(
    *,
    names: list[str] | None = None,
    patterns: list[str] | None = None,
) -> None:
    """Merge plan-safe declarations from a per-server config block.

    ``names`` is a list of literal tool names (preferred when the server's
    read surface is enumerable). ``patterns`` is a list of fnmatch globs.
    Idempotent: registering the same name twice is a no-op. Thread-safe.
    """
    with _lock:
        if names:
            for name in names:
                MCP_PLAN_SAFE_NAMES.add(name)
        if patterns:
            for pattern in patterns:
                if pattern not in MCP_PLAN_SAFE_PATTERNS:
                    MCP_PLAN_SAFE_PATTERNS.append(pattern)


def reset_for_tests() -> None:
    """Reset the runtime classifier additions back to the module defaults.

    Tests that mutate the global classifier should call this in tearDown
    so they don't leak plan-safe entries into sibling tests.
    """
    with _lock:
        MCP_PLAN_SAFE_PATTERNS.clear()
        MCP_PLAN_SAFE_NAMES.clear()
        MCP_PLAN_SAFE_NAMES.update(_BASELINE_NAMES)


# Snapshot taken once, after the module-level mutable set is populated
# above. ``reset_for_tests`` rolls back to this baseline.
_BASELINE_NAMES: frozenset[str] = frozenset(MCP_PLAN_SAFE_NAMES)
