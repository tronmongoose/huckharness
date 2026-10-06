"""Session envelope — deny-by-default scope for what a session may touch.

The envelope is *owner intent*: a session starts able to touch only what its
grants allow, and everything else is denied until an operator grants it (the
JIT flow in ``core.permissions``). This is distinct from the Sentinel gate,
which is the *machine safety* backstop — Sentinel decides what is never safe to
run; the envelope decides what THIS session was scoped to do right now. In the
dispatch chain the envelope gate sits after plan-mode visibility and before
Sentinel: envelope-denied calls never reach Sentinel at all.

A ``ToolRegistry`` with ``envelope is None`` has the gate disabled. Print and
serve attach ``preset(cwd, level, settings)`` by default (print honors
``HARNESS_ENVELOPE=off`` to restore None); repl and direct ``Session`` callers
that pass no envelope keep the gate disabled.
"""
from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from coding_harness.core.mode import Autonomy

if TYPE_CHECKING:
    from coding_harness.core.settings import Settings

# Which argument key each tool exposes its target path under, and whether the
# access it needs is read or write. Tools absent from this map are gated at the
# tool-name level only (no path check) — Bash included, since a shell command
# has no single parseable path (Sentinel reviews the command text separately).
_READ_TOOLS = {"Read": "file_path", "Grep": "path", "Glob": "path"}
_WRITE_TOOLS = {"Write": "file_path", "Edit": "file_path"}
_EXECUTE_TOOLS = {"Bash"}

_VALID_ACCESS = {"read", "write", "execute"}
# Upper bound on an allow_always execute (Bash) grant built by ``grant_for``
# with no operator-set expiry. It exists to stop JIT widening from handing a
# session an unbounded shell; ``preset`` grants are the session's baseline
# scope and are deliberately not bounded by it.
EXECUTE_GRANT_MAX_MINUTES = 15


@dataclass
class Grant:
    """One allowance. ``path_glob`` None means the grant is tool-level (any
    path). ``expires_at`` None means it does not expire on its own."""

    tool: str
    access: str  # "read" | "write" | "execute"
    path_glob: str | None = None
    granted_by: str = "default"  # "default" | "operator"
    expires_at: datetime | None = None

    def is_expired(self, now: datetime) -> bool:
        return self.expires_at is not None and now >= self.expires_at

    def to_dict(self) -> dict[str, object]:
        return {
            "tool": self.tool,
            "access": self.access,
            "path_glob": self.path_glob,
            "granted_by": self.granted_by,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
        }


@dataclass
class EnvelopeDecision:
    allowed: bool
    reason: str
    matched_grant: Grant | None = None


@dataclass
class SessionEnvelope:
    grants: list[Grant] = field(default_factory=list)
    revoked: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    # Canonical roots no write may land under, whatever the grants say
    # (settings.denyWrite).
    deny_write: list[str] = field(default_factory=list)

    def _needed_access(self, tool_name: str, category: str | None = None) -> str:
        """Access a call needs: by built-in name, else by the tool's category."""
        if tool_name in _WRITE_TOOLS:
            return "write"
        if tool_name in _EXECUTE_TOOLS:
            return "execute"
        if category == "execute":
            return "execute"
        if category == "edit":
            return "write"
        return "read"

    def _target_path(self, tool_name: str, args: dict) -> str | None:
        key = _READ_TOOLS.get(tool_name) or _WRITE_TOOLS.get(tool_name)
        if key is None:
            return None
        val = args.get(key)
        if not isinstance(val, str):
            return None
        if "\x00" in val:
            # realpath raises on an embedded null; the raw value is returned
            # so check() denies it rather than treating the call as pathless.
            return val
        # Canonicalize BEFORE matching: realpath collapses ``..`` and resolves
        # symlinks, so a grant on /tmp/** cannot be escaped via
        # "/tmp/../etc/x" or a symlink planted inside the granted dir (the OS
        # would otherwise collapse the path only at write time, landing the
        # file outside scope). This is the load-bearing line of the envelope.
        return os.path.realpath(os.path.expanduser(val))

    def is_live(self) -> bool:
        """Not revoked and not past its own expiry. A hard precondition for
        any grant to apply — checked here and re-checked after a JIT grant to
        close the revoke-during-approval race."""
        if self.revoked:
            return False
        if self.expires_at is not None and datetime.now(timezone.utc) >= self.expires_at:
            return False
        return True

    def check(
        self, tool_name: str, args: dict, *, category: str | None = None
    ) -> EnvelopeDecision:
        """Deny-by-default: allow only if a live grant matches this call.

        ``category`` (``Tool.category``) is the fallback when no grant names
        the tool: a read-category call that carries no path (MCP read tools,
        Grep/Glob defaulting to cwd) passes on any live read-capable grant
        covering cwd. Edit and execute get no fallback, so an MCP execute
        tool is denied unless a tool-level grant names it."""
        now = datetime.now(timezone.utc)
        if self.revoked:
            return EnvelopeDecision(False, "envelope_revoked")
        if self.expires_at is not None and now >= self.expires_at:
            return EnvelopeDecision(False, "envelope_expired")

        needed = self._needed_access(tool_name, category)
        path = self._target_path(tool_name, args)
        if path is not None and "\x00" in path:
            return EnvelopeDecision(False, "invalid_path:embedded_null")
        if needed == "write" and path is not None:
            denied = self._deny_write_root(path)
            if denied is not None:
                return EnvelopeDecision(False, f"deny_write:{denied}")
        for g in self.grants:
            if g.tool != tool_name or g.is_expired(now):
                continue
            # A write grant implies read on the same scope; execute is exact.
            if not _access_satisfies(g.access, needed):
                continue
            if g.path_glob is None:
                return EnvelopeDecision(True, "grant_tool_level", g)
            if path is not None and _path_matches(path, g.path_glob):
                return EnvelopeDecision(True, "grant_path_match", g)
        if category == "read" and path is None:
            fallback = self._read_grant_covering_cwd(now)
            if fallback is not None:
                return EnvelopeDecision(True, "grant_category_read", fallback)
        return EnvelopeDecision(
            False,
            f"out_of_envelope:{tool_name}:{needed}" + (f":{path}" if path else ""),
        )

    def _deny_write_root(self, path: str) -> str | None:
        """The deny_write root containing ``path``, if any."""
        for root in self.deny_write:
            if path == root or path.startswith(root + os.sep):
                return root
        return None

    def _read_grant_covering_cwd(self, now: datetime) -> Grant | None:
        """A live read-capable grant that is tool-level or scoped over cwd."""
        cwd = os.path.realpath(os.getcwd())
        for g in self.grants:
            if g.is_expired(now) or not _access_satisfies(g.access, "read"):
                continue
            if g.path_glob is None or _path_matches(cwd, g.path_glob):
                return g
        return None

    def add_grant(self, grant: Grant) -> None:
        self.grants.append(grant)

    def to_dict(self) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        return {
            "revoked": self.revoked,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "grants": [
                g.to_dict() for g in self.grants if not g.is_expired(now)
            ],
            "deny_write": list(self.deny_write),
        }


def _access_satisfies(granted: str, needed: str) -> bool:
    """A write grant covers read on the same scope; otherwise exact match."""
    if granted == needed:
        return True
    return granted == "write" and needed == "read"


def _path_matches(path: str, glob: str) -> bool:
    """Match a realpath'd target against a grant glob. ``**`` matches across
    separators. Both sides are canonicalized so symlinked roots (macOS
    ``/tmp`` → ``/private/tmp``) and ``..`` segments compare consistently.

    ``path`` is expected to already be realpath'd by ``_target_path``.
    """
    glob = os.path.expanduser(glob)
    if glob.endswith("/**"):
        # Recursive-prefix grant: realpath the concrete prefix and require the
        # target to be contained within it. Containment on canonical paths is
        # the only traversal-safe check.
        prefix = os.path.realpath(glob[:-3].rstrip("/"))
        return path == prefix or path.startswith(prefix + os.sep)
    # Non-recursive glob (rare — presets use /** ). Match the canonical target
    # against both the raw and realpath'd glob so a legitimate exact path still
    # matches while a traversal cannot re-enter scope.
    if fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(path, os.path.realpath(glob)):
        return True
    return False


def preset(
    cwd: str, level: Autonomy = Autonomy.LOW, settings: Settings | None = None,
) -> SessionEnvelope:
    """Baseline scope for a level: read under cwd and ``settings.extraReadRoots``
    at every level; write under cwd plus the repo's memory directory
    (``context.memory.memory_dir``) and Bash at tool level from LOW up.
    ``settings.denyWrite`` roots stay closed to writes whatever the grants say.
    None of these grants expire; the session's own expiry (if any) bounds
    them. ``EXECUTE_GRANT_MAX_MINUTES`` applies only to the ``allow_always``
    widening built by ``grant_for``."""
    root = os.path.realpath(cwd)
    extra = settings.extra_read_roots if settings is not None else []
    deny = settings.deny_write if settings is not None else []
    read_roots = [root] + [os.path.realpath(os.path.expanduser(r)) for r in extra]
    grants = [
        Grant(tool=tool, access="read", path_glob=f"{r}/**")
        for tool in _READ_TOOLS
        for r in read_roots
    ]
    if level is not Autonomy.OFF:
        # Imported here so core carries no context-package import at load time.
        from coding_harness.context.memory import memory_dir

        mem_root = os.path.realpath(str(memory_dir(cwd)))
        write_roots = [root] + ([mem_root] if mem_root != root else [])
        grants += [
            Grant(tool=tool, access="write", path_glob=f"{r}/**")
            for tool in _WRITE_TOOLS
            for r in write_roots
        ]
        grants.append(Grant(tool="Bash", access="execute"))
    return SessionEnvelope(
        grants=grants,
        deny_write=[os.path.realpath(os.path.expanduser(r)) for r in deny],
    )


def grant_for(
    tool_name: str,
    args: dict,
    *,
    expiry_minutes: object = None,
    granted_by: str = "operator",
    now: datetime | None = None,
) -> Grant:
    """Build the Grant that would let ``tool_name(args)`` pass ``check``.

    Used by the JIT broker's ``allow_always`` path: it widens the envelope to
    exactly the call the operator approved — the tool, its needed access, and
    its concrete target path (tool-level for Bash / unknown, which carry no
    single parseable path). ``expiry_minutes`` bounds the grant's life.

    Execute (Bash) grants are tool-level with no path scope, so a persistent
    ``allow_always`` on one benign command would otherwise hand the session an
    unbounded shell forever. Such grants are force-bounded to
    ``EXECUTE_GRANT_MAX_MINUTES`` when the operator leaves expiry unset.
    """
    now = now or datetime.now(timezone.utc)
    if tool_name in _WRITE_TOOLS:
        access = "write"
    elif tool_name in _EXECUTE_TOOLS:
        access = "execute"
    else:
        access = "read"
    key = _READ_TOOLS.get(tool_name) or _WRITE_TOOLS.get(tool_name)
    path_glob: str | None = None
    if key is not None:
        val = args.get(key)
        if isinstance(val, str):
            # Store the canonical path so the grant means the same file the
            # traversal-safe check() resolves the call to.
            path_glob = os.path.realpath(os.path.expanduser(val))
    if access == "execute" and expiry_minutes is None:
        expiry_minutes = EXECUTE_GRANT_MAX_MINUTES
    return Grant(
        tool=tool_name,
        access=access,
        path_glob=path_glob,
        granted_by=granted_by,
        expires_at=_plus_minutes(now, expiry_minutes),
    )


class EnvelopeSpecError(ValueError):
    """A malformed envelope spec from the API. Callers map it to HTTP 400."""


def envelope_from_dict(data: dict) -> SessionEnvelope:
    """Build an envelope from the JSON shape the serve API accepts.

    Shape: ``{"grants": [{"tool","access","path_glob"?,"expiry_minutes"?}],
    "expiry_minutes"?}``. Missing access defaults to read. Validates every
    field so a malformed grant raises ``EnvelopeSpecError`` (→ 400) instead of
    500-ing at construction or crashing a turn later inside ``check()`` (a
    non-string ``path_glob`` would raise deep in dispatch).
    """
    now = datetime.now(timezone.utc)
    raw_grants = data.get("grants", []) or []
    if not isinstance(raw_grants, list):
        raise EnvelopeSpecError("envelope.grants must be a list")
    grants: list[Grant] = []
    for i, g in enumerate(raw_grants):
        if not isinstance(g, dict):
            raise EnvelopeSpecError(f"grant[{i}] must be an object")
        tool = g.get("tool")
        if not isinstance(tool, str) or not tool:
            raise EnvelopeSpecError(f"grant[{i}].tool is required (string)")
        access = g.get("access", "read")
        if access not in _VALID_ACCESS:
            raise EnvelopeSpecError(
                f"grant[{i}].access must be one of {sorted(_VALID_ACCESS)}"
            )
        path_glob = g.get("path_glob")
        if path_glob is not None and not isinstance(path_glob, str):
            raise EnvelopeSpecError(f"grant[{i}].path_glob must be a string")
        grants.append(Grant(
            tool=tool,
            access=access,
            path_glob=path_glob,
            granted_by=g.get("granted_by", "default"),
            expires_at=_plus_minutes(now, _valid_minutes(g.get("expiry_minutes"), i)),
        ))
    return SessionEnvelope(
        grants=grants,
        expires_at=_plus_minutes(now, _valid_minutes(data.get("expiry_minutes"), None)),
    )


def _valid_minutes(minutes: object, grant_idx: int | None) -> object:
    """Reject non-positive / non-numeric expiries (a negative expiry silently
    creates an already-dead grant that misleads the operator)."""
    if minutes is None:
        return None
    where = f"grant[{grant_idx}].expiry_minutes" if grant_idx is not None else "expiry_minutes"
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
        raise EnvelopeSpecError(f"{where} must be a positive number")
    if minutes <= 0:
        raise EnvelopeSpecError(f"{where} must be positive")
    return minutes


def _plus_minutes(now: datetime, minutes: object) -> datetime | None:
    if minutes is None:
        return None
    from datetime import timedelta
    return now + timedelta(minutes=float(minutes))  # type: ignore[arg-type]


def envelope_from_state_dict(data: dict) -> SessionEnvelope:
    """Rebuild an envelope from ``to_dict`` output — absolute times preserved.

    The inverse of ``to_dict``, NOT of ``envelope_from_dict``: that one takes
    operator-facing *relative* ``expiry_minutes`` and starts the clock now.
    Feeding a recorded envelope through it would restart every window, so a
    resumed session would silently outlive its original grants. Here the
    stored ``expires_at`` instants are carried across verbatim, so a resume
    inherits exactly the authority the session already had — and an envelope
    that lapsed while the process was down stays lapsed.

    Note ``to_dict`` filters already-expired grants, so a rehydrated envelope
    holds only the grants that were still live when it was recorded.
    """
    grants = []
    for raw in data.get("grants", []) or []:
        grants.append(Grant(
            tool=str(raw["tool"]),
            access=str(raw.get("access", "read")),
            path_glob=raw.get("path_glob"),
            granted_by=str(raw.get("granted_by", "default")),
            expires_at=_parse_iso(raw.get("expires_at")),
        ))
    created = _parse_iso(data.get("created_at")) or datetime.now(timezone.utc)
    return SessionEnvelope(
        grants=grants,
        revoked=bool(data.get("revoked", False)),
        created_at=created,
        expires_at=_parse_iso(data.get("expires_at")),
        deny_write=[str(r) for r in data.get("deny_write", []) or []],
    )


def _parse_iso(value: object) -> datetime | None:
    """Parse an ISO timestamp from a stored envelope; None stays None."""
    if not value or not isinstance(value, str):
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
