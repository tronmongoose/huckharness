"""JIT permission broker — the human-in-the-loop grant for out-of-envelope calls.

When ``ToolRegistry.dispatch`` hits a tool call the session envelope denies, and
a broker is attached, it calls ``broker.request(...)``. That blocks the turn
thread on a ``threading.Event`` while emitting a ``permission_request`` event
for any listener (the SSE stream). An operator resolves it out-of-band via
``resolve(req_id, decision)`` — from a different HTTP handler thread — which
sets the event and unblocks the turn.

Threading contract (safe under stdlib ThreadingHTTPServer): the turn thread
holds the per-session turn lock and blocks in ``request().event.wait()``; the
resolving thread touches only ``self.pending`` under ``self.lock`` and never
needs the turn lock, so there is no deadlock and turn serialization holds.

Fail-closed by construction: ``request`` has a hard timeout that resolves to
DENY, and ``list_pending`` is the source of truth so a dropped SSE event
degrades to poll latency, not a stuck turn.
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from coding_harness.core.git import DIFF_CAP

DEFAULT_TIMEOUT_S = 300


@dataclass
class BrokerDecision:
    allowed: bool
    decision: str  # "allow_once" | "allow_always" | "deny" | "timeout"
    expiry_minutes: int | None = None


@dataclass
class PendingRequest:
    req_id: str
    tool: str
    args: dict[str, Any]
    reason: str
    created_at: datetime
    event: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None
    expiry_minutes: int | None = None
    # "permission": an out-of-envelope call. "review": a write the operator
    # asked to see before it lands; its allow_always never widens the envelope.
    kind: str = "permission"

    # Arg keys whose FULL value the operator must see to make a safe decision:
    # the exact path a Write lands on and the exact shell command. Truncating
    # these would let an approval be granted on a preview that differs from
    # what actually executes (a benign 200-char head hiding a malicious tail).
    _FULL_ARG_KEYS = ("file_path", "path", "command")
    _PREVIEW_CAP = 2000

    def to_dict(self) -> dict[str, Any]:
        preview = {}
        for k, v in self.args.items():
            s = str(v)
            if k in self._FULL_ARG_KEYS:
                preview[k] = s[: self._PREVIEW_CAP]
            elif k == "diff":
                preview[k] = s[:DIFF_CAP]
            else:
                preview[k] = s[:200]
        return {
            "req_id": self.req_id,
            "tool": self.tool,
            "args_preview": preview,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
            "kind": self.kind,
        }


class PermissionBroker:
    """Per-session broker. ``emit`` fans an event dict out to listeners.

    ``on_grant`` is called with (PendingRequest) when a request resolves to a
    persistent grant (allow_always) so the caller can widen the envelope +
    write the audit row before the turn resumes.
    """

    def __init__(
        self,
        emit: Callable[[str, dict[str, Any]], None],
        on_grant: Callable[[PendingRequest], None] | None = None,
    ) -> None:
        self.emit = emit
        self.on_grant = on_grant
        self.pending: dict[str, PendingRequest] = {}
        self.lock = threading.Lock()

    def request(
        self,
        tool: str,
        args: dict[str, Any],
        reason: str,
        timeout: float = DEFAULT_TIMEOUT_S,
        kind: str = "permission",
    ) -> BrokerDecision:
        req = PendingRequest(
            req_id=uuid.uuid4().hex[:12],
            tool=tool,
            args=args,
            reason=reason,
            created_at=datetime.now(timezone.utc),
            kind=kind,
        )
        with self.lock:
            self.pending[req.req_id] = req
        self.emit("permission_request", req.to_dict())

        req.event.wait(timeout)
        # Read the decision and remove from pending under one lock so a
        # resolve() landing in the wait/pop window can't leave a "decided but
        # timed out" ambiguity: whichever of (decision set, timeout) is true
        # under the lock wins, atomically.
        with self.lock:
            self.pending.pop(req.req_id, None)
            decision = req.decision

        if decision is None:
            # No decision was recorded before the wait returned ⇒ genuine
            # timeout (fail-closed deny). ``got`` may be True if resolve() set
            # the event but a second resolver cleared decision — treat absence
            # of a decision as deny regardless.
            self.emit("permission_timeout", {"req_id": req.req_id, "tool": tool})
            return BrokerDecision(False, "timeout")

        if decision == "allow_always" and kind == "permission" and self.on_grant is not None:
            self.on_grant(req)
        return BrokerDecision(
            allowed=decision in ("allow_once", "allow_always"),
            decision=decision,
            expiry_minutes=req.expiry_minutes,
        )

    def resolve(
        self, req_id: str, decision: str, expiry_minutes: int | None = None
    ) -> bool:
        """Resolve a pending request. Returns False if unknown/already resolved.

        Removes the request from ``pending`` atomically so a second
        ``resolve()`` on the same id (double-click, two operator tabs) returns
        False rather than overwriting the first decision — the first decision
        is final. The waiting turn thread still reads ``req.decision`` under
        the same lock in ``request()``.
        """
        with self.lock:
            req = self.pending.pop(req_id, None)
            if req is None:
                return False
            req.decision = decision
            req.expiry_minutes = expiry_minutes
        req.event.set()
        self.emit("permission_resolved", {"req_id": req_id, "decision": decision, "kind": req.kind})
        return True

    def deny_all(self, reason: str = "revoked") -> int:
        """Deny every pending request now — used by interrupt/revoke so a turn
        parked in ``request()`` unblocks immediately instead of waiting out the
        300s timeout. Returns how many were denied."""
        with self.lock:
            reqs = list(self.pending.values())
            for req in reqs:
                req.decision = "deny"
            self.pending.clear()
        for req in reqs:
            req.event.set()
            self.emit("permission_resolved", {"req_id": req.req_id, "decision": "deny", "kind": req.kind})
        return len(reqs)

    def list_pending(self) -> list[dict[str, Any]]:
        with self.lock:
            return [r.to_dict() for r in self.pending.values()]
