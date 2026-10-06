"""Tool registry — the single dispatch point for every tool call.

Every model-issued tool call MUST go through ``ToolRegistry.dispatch``. There
is no second path. The dispatch sequence is:

    Sentinel review (in-process security.policy + optional deployment hook)
        ├─ blocked → audit + return ToolResult(is_error=True)
        └─ allowed → if PlannedTool:
                         plan(args)
                             ├─ validation error → audit + return error
                             └─ ok →
                                 confirm_callback?(plan)
                                     ├─ False → audit(denied_by_operator) +
                                     │            return "denied by operator"
                                     └─ True (or no callback) →
                                         snapshot_callback?(plan)
                                         apply(plan)
                     else:
                         tool.run(args)
                 → audit + return result

This is the deliberate divergence from pi-mono's "permissions are an extension"
stance. The Sentinel gate is core, not optional, and not removable. The
operator-confirm gate (Erik's diff preview) sits on top of Sentinel: Sentinel
catches what the *machine* shouldn't run, the operator gate catches what *this*
operator doesn't want to run *right now*.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from coding_harness.core.mode import Autonomy, Mode, is_plan_safe
from coding_harness.core.permissions import DEFAULT_TIMEOUT_S
from coding_harness.security import audit, policy, sentinel
from coding_harness.tools.base import PlannedTool, Tool, ToolResult, WritePlan

if TYPE_CHECKING:
    # Lazy at runtime — importing the registry shouldn't pull subprocess
    # and threading machinery for non-MCP code paths.
    from coding_harness.core.envelope import SessionEnvelope
    from coding_harness.core.permissions import BrokerDecision, PermissionBroker
    from coding_harness.core.settings import Settings
    from coding_harness.mcp.client import MCPClient

TOOL_RESULT_EVENT_MAX = 6000


@dataclass
class DispatchEvent:
    """Event yielded for every dispatch attempt. Print mode and (later) RPC
    mode both consume these to surface what's happening to the operator."""

    kind: str  # "sentinel_verdict" | "tool_call_start" | "tool_call_result" | "audit_entry"
    payload: dict[str, Any]


# Confirm callback contract: receives the WritePlan, returns True to apply,
# False to reject. The callback may also surface the diff to the operator,
# block on input, etc. Print mode passes None (auto-accept) or a "always False"
# callback (--dry-run). REPL mode passes a function that prints the diff and
# reads y/N.
ConfirmCallback = Callable[[WritePlan], bool]

# Snapshot callback contract: receives the WritePlan after confirm, before
# apply. Session-level closure that knows the current turn + seq.
SnapshotCallback = Callable[[WritePlan], None]


@dataclass
class ToolRegistry:
    tools: dict[str, Tool] = field(default_factory=dict)
    session_id: str = "unknown"
    event_sink: Callable[[DispatchEvent], None] | None = None
    confirm_callback: ConfirmCallback | None = None
    snapshot_callback: SnapshotCallback | None = None
    # MCP servers spawned via register_mcp_server. The registry owns the
    # close-on-teardown lifecycle so Session.close drains every subprocess
    # without modes having to track them separately.
    mcp_clients: list[Any] = field(default_factory=list)
    # Plan/Act mode. When PLAN, ``to_openai_tools`` and
    # ``dispatch`` filter out act-only tools so the model literally cannot
    # see or call them. Default ACT preserves pre-existing behavior for
    # every caller that doesn't opt into Plan mode.
    mode: Mode = Mode.ACT
    # Session envelope (deny-by-default scope). None ⇒ gate disabled, so
    # every pre-existing caller is unaffected. When set, out-of-envelope
    # calls are either brokered (if permission_broker is set) or hard-denied.
    envelope: SessionEnvelope | None = None
    permission_broker: PermissionBroker | None = None
    # Autonomy level and the settings the command classifier consults; both
    # reach Sentinel on every dispatch. An 'ask' verdict is brokered like an
    # out-of-envelope call when a broker is attached, else hard-denied.
    autonomy: Autonomy = Autonomy.LOW
    settings: Settings | None = None
    # Agent identity stamped onto every audit row. Display + chain
    # provenance; per-action signing is a later phase.
    agent_identity: str | None = None
    # Why a configured second brain could not start; the mode reports it once.
    brain_error: str | None = None
    # Settings-declared lifecycle hooks (core.hooks.HookRunner). None keeps
    # dispatch hook-free; when set, PreToolUse can deny a call and
    # PostToolUse observes every result.
    hooks: Any | None = None
    # Wall-clock (time.time) instant the session must finish by; None means
    # unbounded. Set by the session deadline owner; here it only caps how long
    # a brokered permission wait may park a turn.
    deadline_at: float | None = None

    def deadline_remaining_s(self) -> float:
        """Seconds until ``deadline_at``; unbounded when unset."""
        if self.deadline_at is None:
            return float("inf")
        return max(0.0, self.deadline_at - time.time())

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError(f"tool {tool!r} has no .name")
        # A tool that runs subprocesses (Bash) declares this slot so its own
        # timeout can be clamped to the turn deadline the registry mirrors.
        if hasattr(tool, "deadline_remaining_s") and tool.deadline_remaining_s is None:
            tool.deadline_remaining_s = self.deadline_remaining_s
        self.tools[tool.name] = tool

    def register_mcp_server(self, client: MCPClient) -> list[str]:
        """Start ``client`` if not already started, list its tools, register
        each as an ``MCPTool``. Returns the list of registered tool names.

        Lifecycle: the registry retains the client; ``close_mcp_clients``
        terminates them in reverse order on teardown. Failure to start the
        client raises ``MCPClientError`` — the caller decides whether that's
        fatal (default) or downgradable (e.g., ``--no-mcp`` from the CLI).

        Per-server plan-safe declarations from ``client.config``
        are merged into the global ``core.mode`` classifier here. This is
        the single point where a server's "I have these read-only tools"
        intent crosses into the live Plan/Act gate.
        """
        from coding_harness.core.mode import register_mcp_plan_safe
        from coding_harness.mcp.tool_adapter import MCPTool  # local import

        if not client.is_alive():
            client.start()
        descriptors = client.list_tools()
        registered: list[str] = []
        for descriptor in descriptors:
            tool = MCPTool.from_descriptor(client, descriptor)
            self.register(tool)
            registered.append(tool.name)
        cfg = client.config
        if cfg.plan_safe_names or cfg.plan_safe_patterns:
            register_mcp_plan_safe(
                names=list(cfg.plan_safe_names),
                patterns=list(cfg.plan_safe_patterns),
            )
        self.mcp_clients.append(client)
        return registered

    def close_mcp_clients(self) -> None:
        """Close every MCP server attached via ``register_mcp_server``.

        Idempotent. Safe to call from ``Session.close`` and from ``atexit``
        hooks. Closing in reverse registration order so later servers that
        depend on earlier ones (none today, but reserve the option) shut
        down first.
        """
        while self.mcp_clients:
            client = self.mcp_clients.pop()
            try:
                client.close()
            except Exception:  # noqa: BLE001 — teardown must not raise
                pass

    def to_openai_tools(self) -> list[dict[str, Any]]:
        return [
            t.to_openai_tool()
            for t in self.tools.values()
            if self._visible(t.name)
        ]

    def _visible(self, tool_name: str) -> bool:
        """Whether ``tool_name`` is visible in the current mode.

        Plan mode hides every act-only tool; Act mode shows everything.
        Plan-safety classification lives in ``coding_harness.core.mode`` so
        this stays a single-line predicate.
        """
        if self.mode is Mode.ACT:
            return True
        return is_plan_safe(tool_name)

    def _emit(self, kind: str, payload: dict[str, Any]) -> None:
        if self.event_sink is not None:
            self.event_sink(DispatchEvent(kind=kind, payload=payload))

    def dispatch(self, tool_name: str, args: dict[str, Any]) -> ToolResult:
        """The only entry point. Sentinel-gated, audited, event-streaming."""
        tool = self.tools.get(tool_name)
        if tool is None:
            # Refuse before Sentinel even sees it. Still audit so the bypass
            # attempt is on the chain.
            result = ToolResult(
                content=f"unknown tool: {tool_name}",
                is_error=True,
            )
            entry = audit.append(
                session_id=self.session_id,
                tool=tool_name,
                args=args,
                result=result.content,
                allowed=False,
                sentinel_reason="tool not registered",
                sentinel_path="registry",
                error="unknown_tool",
                agent_identity=self.agent_identity,
            )
            self._emit("audit_entry", entry)
            return result

        # Plan-mode visibility gate. The model shouldn't be able
        # to see act-only tools when mode is PLAN, but a small local model
        # may hallucinate a Bash call from training memory. Refuse before
        # Sentinel sees it; still audit so the attempt is recorded.
        if not self._visible(tool.name):
            result = ToolResult(
                content=(
                    f"BLOCKED in plan mode: '{tool.name}' is act-only. "
                    "Use only read tools, or ask the operator to switch to act mode."
                ),
                is_error=True,
                metadata={"plan_mode_block": True},
            )
            entry = audit.append(
                session_id=self.session_id,
                tool=tool.name,
                args=args,
                result=result.content,
                allowed=False,
                sentinel_reason="plan_mode_act_only",
                sentinel_path="mode",
                agent_identity=self.agent_identity,
            )
            self._emit("audit_entry", entry)
            return result

        # Envelope gate (deny-by-default scope). Owner intent, distinct from
        # Sentinel's machine-safety backstop. Sits before Sentinel so an
        # out-of-envelope call never reaches it. If a broker is attached, an
        # operator can grant JIT; otherwise it's a hard deny.
        blocked = self._envelope_gate(tool, args)
        if blocked is not None:
            return blocked

        # 1) Sentinel review
        verdict = sentinel.review(
            tool_name=tool.name,
            tool_input=args,
            session_id=self.session_id,
            category=tool.category,
            cwd=os.getcwd(),
            level=self.autonomy,
            settings=self.settings,
        )
        self._emit("sentinel_verdict", {
            "tool": tool.name,
            "allowed": verdict.allowed,
            "reason": verdict.reason,
            "path": verdict.path,
        })

        if not verdict.allowed and verdict.path == policy.PATH_ASK:
            outcome = self._broker_or_deny(
                tool, args, verdict.reason,
                gate="autonomy", audit_reason=verdict.reason, audit_path=verdict.path,
            )
            if isinstance(outcome, ToolResult):
                return outcome
        elif not verdict.allowed:
            result = ToolResult(
                content=f"BLOCKED by Sentinel: {verdict.reason}",
                is_error=True,
                metadata={"sentinel_reason": verdict.reason},
            )
            entry = audit.append(
                session_id=self.session_id,
                tool=tool.name,
                args=args,
                result=result.content,
                allowed=False,
                sentinel_reason=verdict.reason,
                sentinel_path=verdict.path,
                agent_identity=self.agent_identity,
            )
            self._emit("audit_entry", entry)
            return result

        if self.hooks is not None:
            hook_decision = self.hooks.run(
                "PreToolUse", tool_name=tool.name, tool_input=args,
            )
            if not hook_decision.allowed:
                return self._deny(tool, args, hook_decision.reason, gate="hook")

        # 2) Tool body
        self._emit("tool_call_start", {"tool": tool.name, "args": args})
        result, error, denied_by_operator = self._run_tool(tool, args)
        if self.hooks is not None:
            self.hooks.run(
                "PostToolUse",
                tool_name=tool.name, tool_input=args, tool_output=result.content,
            )

        self._emit("tool_call_result", {
            "tool": tool.name,
            "is_error": result.is_error,
            "denied_by_operator": denied_by_operator,
            "content_preview": result.content[:200],
            # Bounded so a GUI can expand the step without a second fetch.
            "content": result.content[:TOOL_RESULT_EVENT_MAX],
            "error_class": result.metadata.get("error_class"),
        })

        # 3) Audit
        entry = audit.append(
            session_id=self.session_id,
            tool=tool.name,
            args=args,
            result=result.content,
            allowed=True,
            sentinel_reason=verdict.reason,
            sentinel_path=verdict.path,
            error=error,
            denied_by_operator=denied_by_operator,
            carryall_audit_id=_extract_carryall_audit_id(result),
            agent_identity=self.agent_identity,
        )
        self._emit("audit_entry", entry)

        return result

    def _envelope_gate(
        self, tool: Tool, args: dict[str, Any]
    ) -> ToolResult | None:
        """Deny-by-default scope check. Returns None to proceed, or an error
        ToolResult to short-circuit dispatch.

        None envelope ⇒ gate disabled (pre-existing callers). On deny with a
        broker attached, block for a JIT grant; without a broker (or on
        deny/timeout), hard-deny.
        """
        if self.envelope is None:
            return None

        decision = self.envelope.check(tool.name, args, category=tool.category)
        reason = decision.reason
        if decision.allowed:
            return None

        # A revoked or expired envelope is a hard stop, not a JIT-approvable
        # request: never broker it (else revoke degrades to "every call is
        # re-approvable"). Only an out-of-scope call inside a live envelope
        # is escalated to the operator.
        if reason in ("envelope_revoked", "envelope_expired"):
            return self._deny(tool, args, reason, gate="envelope")
        outcome = self._broker_or_deny(tool, args, reason, gate="envelope")
        if isinstance(outcome, ToolResult):
            return outcome
        # allow_always widened the envelope via on_grant; allow_once is a
        # single-shot pass. Re-check to close the revoke/expire race: if the
        # envelope was revoked WHILE the operator was deciding, the grant must
        # not resurrect it (a concurrent /revoke sets revoked=True; the broker
        # approval must not override it). allow_once adds no grant, so its
        # re-check denies by construction and is honored only while live.
        recheck = self.envelope.check(tool.name, args, category=tool.category)
        if recheck.allowed or (outcome.decision == "allow_once" and self.envelope.is_live()):
            return None
        return self._deny(tool, args, recheck.reason, gate="envelope")

    def _broker_or_deny(
        self,
        tool: Tool,
        args: dict[str, Any],
        reason: str,
        *,
        gate: str,
        audit_reason: str | None = None,
        audit_path: str | None = None,
    ) -> BrokerDecision | ToolResult:
        """Park the call with the broker and return the operator's approval;
        an audited deny when no broker is attached or the operator refused."""
        decision = None
        if self.permission_broker is not None:
            decision = self.permission_broker.request(
                tool=tool.name,
                args=args,
                reason=reason,
                timeout=min(DEFAULT_TIMEOUT_S, self.deadline_remaining_s()),
            )
        if decision is not None and decision.allowed:
            return decision
        return self._deny(
            tool, args, reason, gate=gate, audit_reason=audit_reason, audit_path=audit_path,
        )

    def _deny(
        self,
        tool: Tool,
        args: dict[str, Any],
        reason: str,
        *,
        gate: str,
        audit_reason: str | None = None,
        audit_path: str | None = None,
    ) -> ToolResult:
        """Audited error result for a call ``gate`` refused."""
        result = ToolResult(
            content=f"BLOCKED by {gate}: {reason}",
            is_error=True,
            metadata={f"{gate}_denied": True, "reason": reason},
        )
        entry = audit.append(
            session_id=self.session_id,
            tool=tool.name,
            args=args,
            result=result.content,
            allowed=False,
            sentinel_reason=audit_reason or f"{gate}_denied",
            sentinel_path=audit_path or gate,
            agent_identity=self.agent_identity,
        )
        self._emit("audit_entry", entry)
        return result

    def _run_tool(
        self, tool: Tool, args: dict[str, Any]
    ) -> tuple[ToolResult, str | None, bool]:
        """Run a single tool through the plan/confirm/snapshot/apply pipeline
        (PlannedTool) or directly via run() (plain Tool).

        Returns ``(result, error_string_or_None, denied_by_operator)``.
        """
        try:
            if isinstance(tool, PlannedTool):
                outcome = tool.plan(args)
                if isinstance(outcome, ToolResult):
                    # Validation error in plan(); behave like a tool error.
                    return outcome, None, False

                plan = outcome
                self._emit("plan_ready", {
                    "tool": tool.name,
                    "file_path": str(plan.file_path),
                    "summary": plan.summary,
                    "diff": plan.unified_diff,
                    "existed": plan.existed,
                })

                if self.confirm_callback is not None:
                    approved = self.confirm_callback(plan)
                    if not approved:
                        return (
                            ToolResult(
                                content=f"denied by operator: {plan.summary}",
                                is_error=False,
                                metadata={"denied_by_operator": True, "diff": plan.unified_diff},
                            ),
                            None,
                            True,
                        )

                if self.snapshot_callback is not None:
                    self.snapshot_callback(plan)

                return tool.apply(plan), None, False

            result = tool.run(args)
            # TodoWrite hands its validated list back in metadata; the GUI
            # renders it live, so it rides the event stream like plan_ready.
            if "todo_items" in result.metadata:
                self._emit("todo_update", {"items": result.metadata["todo_items"]})
            return result, None, False
        except Exception as e:  # noqa: BLE001 — the registry is the firewall
            return (
                ToolResult(content=f"tool raised {type(e).__name__}: {e}", is_error=True),
                f"{type(e).__name__}: {e}",
                False,
            )


def _extract_carryall_audit_id(result: ToolResult) -> str | None:
    value = result.metadata.get("carryall_audit_id")
    if isinstance(value, (str, int)) and str(value):
        return str(value)
    return None
