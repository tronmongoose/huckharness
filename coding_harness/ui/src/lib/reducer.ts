// Pure reducer over the serve SSE stream. Kept free of React so the turn
// logic is unit-testable: hooks/useEventStream wires it to an EventSource.

import type {
  AuditRow,
  MemoryProposal,
  PinnedNote,
  RecallInfo,
  PendingPermission,
  ContextUse,
  GitCommitInfo,
  Phase,
  ResumedInfo,
  PhaseKind,
  SpecStatus,
  StreamEvent,
  TodoItem,
  ToolStep,
  TurnBlock,
  TurnItem,
} from "./types";

export interface StreamState {
  thread: TurnBlock[];
  pending: PendingPermission[];
  auditTrail: AuditRow[];
  identity: string | null;
  revoked: boolean;
  turnInFlight: boolean;
  phase: Phase;
  context: ContextUse | null;
  todos: TodoItem[];
  spec: SpecStatus;
  autonomy: string | null; // from autonomy_change; null until one arrives
  // Review-writes state as the last resolved review request left it.
  reviewWrites: boolean | null;
  proposals: MemoryProposal[];
  pinned: PinnedNote[];
  recall: RecallInfo | null;
  suggestedSkills: string[];
  // The latest commit made from the git panel, from git_commit.
  lastCommit: GitCommitInfo | null;
  // Set by session_resumed: this thread was rebuilt from a past transcript.
  resumed: ResumedInfo | null;
}

const IDLE: Phase = { kind: "idle", since: 0, detail: "", tokens: 0 };

function enter(state: StreamState, kind: PhaseKind, detail = ""): StreamState {
  return { ...state, phase: { kind, since: Date.now(), detail, tokens: 0 } };
}

export const initialStreamState: StreamState = {
  thread: [],
  pending: [],
  auditTrail: [],
  identity: null,
  revoked: false,
  turnInFlight: false,
  phase: IDLE,
  context: null,
  todos: [],
  spec: { version: 0, bytes: null, error: null },
  autonomy: null,
  reviewWrites: null,
  proposals: [],
  pinned: [],
  recall: null,
  suggestedSkills: [],
  lastCommit: null,
  resumed: null,
};

const AUDIT_TRAIL_MAX = 100;

function openTurn(state: StreamState): TurnBlock | null {
  const last = state.thread[state.thread.length - 1];
  return last && !last.done ? last : null;
}

function withOpenTurn(
  state: StreamState,
  fn: (t: TurnBlock) => TurnBlock,
): StreamState {
  const t = openTurn(state);
  if (!t) return state;
  const thread = state.thread.slice(0, -1).concat(fn(t));
  return { ...state, thread };
}

// Index of the last step not yet sealed by its audit_entry (which stamps the
// chain hash), or -1. Sealing on the hash means each call's events land on its
// own step even when the same tool is called back-to-back.
function _lastOpen(steps: ToolStep[]): number {
  const i = steps.length - 1;
  return i >= 0 && !steps[i].auditHash ? i : -1;
}

function appendText(items: TurnItem[], text: string): TurnItem[] {
  const last = items[items.length - 1];
  if (last && last.kind === "text") {
    return items.slice(0, -1).concat({ kind: "text", text: last.text + text });
  }
  return items.concat({ kind: "text", text });
}

// Push a new step and its place in the thread order together, so the two
// can never disagree about which index a step holds.
function pushStep(t: TurnBlock, step: ToolStep): TurnBlock {
  return {
    ...t,
    steps: [...t.steps, step],
    items: [...t.items, { kind: "step", index: t.steps.length }],
  };
}

function asArgs(args: unknown): Record<string, unknown> | undefined {
  return args != null && typeof args === "object"
    ? (args as Record<string, unknown>)
    : undefined;
}

function previewArgs(args: unknown): string {
  if (args == null || typeof args !== "object") return "";
  const parts = Object.entries(args as Record<string, unknown>).map(
    ([k, v]) => `${k}: ${String(v).slice(0, 120)}`,
  );
  return parts.join("  ·  ").slice(0, 240);
}

// Status-line phase from the same stream. Kept apart from the thread logic so
// each stays readable; reduceEvent applies both.
function trackPhase(state: StreamState, ev: StreamEvent): StreamState {
  const p = ev.params;
  switch (ev.method) {
    case "turn_start":
      return enter(state, "starting");
    case "checks_baseline_start":
      return enter(state, "baseline", ((p.checks as string[]) ?? []).join(" && "));
    case "model_call_start":
      return enter(state, "waiting", String(p.model ?? ""));
    case "assistant_delta":
      if (state.phase.kind === "generating") {
        return { ...state, phase: { ...state.phase, tokens: state.phase.tokens + 1 } };
      }
      // detail carries over: the model tag from the wait that just ended.
      return {
        ...state,
        phase: { kind: "generating", since: Date.now(), detail: state.phase.detail, tokens: 1 },
      };
    case "tool_call_start":
      return enter(state, "tool", String(p.tool ?? "?"));
    case "permission_request":
      return enter(state, "approval", String(p.tool ?? "?"));
    case "turn_done":
      return { ...state, phase: IDLE };
    default:
      return state;
  }
}

export function reduceEvent(state: StreamState, ev: StreamEvent): StreamState {
  return trackPhase(reduceThread(state, ev), ev);
}

function reduceThread(
  state: StreamState,
  ev: StreamEvent,
): StreamState {
  const p = ev.params;
  switch (ev.method) {
    case "session_start":
      return {
        ...state,
        identity: (p.identity as string | null) ?? state.identity,
      };

    case "turn_start": {
      const block: TurnBlock = {
        turn: Number(p.turn ?? state.thread.length + 1),
        prompt: String(p.prompt ?? ""),
        streamingText: "",
        steps: [],
        items: [],
        done: false,
        checkpoint: (p.checkpoint as string | null | undefined) ?? null,
      };
      return { ...state, thread: [...state.thread, block], turnInFlight: true };
    }

    case "subagent_start":
      return withOpenTurn(state, (t) => {
        const id = String(p.id ?? "");
        const steps = t.steps.slice();
        // The helper belongs to the newest Explore call still waiting on its result.
        for (let i = steps.length - 1; i >= 0; i--) {
          if (steps[i].tool === "Explore" && steps[i].resultPreview === undefined) {
            steps[i] = { ...steps[i], subagentId: id };
            break;
          }
        }
        const agent = { id, agent: String(p.agent ?? "explore"), task: String(p.task ?? ""),
          model: String(p.model ?? ""), steps: [], done: false };
        return { ...t, steps, subagents: { ...t.subagents, [id]: agent } };
      });

    case "subagent_step":
    case "subagent_done":
      return withOpenTurn(state, (t) => {
        const id = String(p.id ?? "");
        const agent = t.subagents?.[id];
        if (!agent) return t;
        const next = ev.method === "subagent_step"
          ? { ...agent, steps: [...agent.steps, { tool: String(p.tool ?? "?"), summary: String(p.summary ?? "") }] }
          : { ...agent, done: true, halted: String(p.halted ?? ""), stepCount: Number(p.steps ?? 0) };
        return { ...t, subagents: { ...t.subagents, [id]: next } };
      });

    case "route_decision":
      return withOpenTurn(state, (t) => ({
        ...t,
        model: String(p.model ?? ""),
        routeReason: String(p.route_reason ?? ""),
      }));

    case "assistant_delta":
      return withOpenTurn(state, (t) => ({
        ...t,
        streamingText: t.streamingText + String(p.text ?? ""),
        items: appendText(t.items, String(p.text ?? "")),
      }));

    // Event order per tool call (from registry.py): sentinel_verdict FIRST,
    // then (if allowed) tool_call_start → tool_call_result → audit_entry.
    // A blocked call (Sentinel/envelope/plan/paste-echo) emits only
    // audit_entry. A step is "open" until its audit_entry stamps a hash;
    // audit_entry seals it. Matching on open-ness, not tool name, keeps
    // repeated same-tool calls and denials from colliding.
    case "sentinel_verdict":
      // The first event of a call opens its step.
      return withOpenTurn(state, (t) =>
        pushStep(t, {
          tool: String(p.tool ?? "?"),
          argsPreview: "",
          sentinelAllowed: Boolean(p.allowed),
          sentinelReason: String(p.reason ?? ""),
        }),
      );

    case "tool_call_start":
      return withOpenTurn(state, (t) => {
        const i = _lastOpen(t.steps);
        if (i < 0 || t.steps[i].argsPreview) {
          return pushStep(t, {
            tool: String(p.tool ?? "?"),
            argsPreview: previewArgs(p.args),
            args: asArgs(p.args),
          });
        }
        const steps = t.steps.slice();
        steps[i] = {
          ...steps[i],
          tool: String(p.tool ?? steps[i].tool),
          argsPreview: previewArgs(p.args),
          args: asArgs(p.args),
        };
        return { ...t, steps };
      });

    case "tool_call_result":
      return withOpenTurn(state, (t) => {
        const steps = t.steps.slice();
        const i = _lastOpen(steps);
        if (i >= 0) {
          steps[i] = {
            ...steps[i],
            isError: Boolean(p.is_error),
            resultPreview: String(p.content_preview ?? ""),
            result: String(p.content ?? p.content_preview ?? ""),
          };
        }
        return { ...t, steps };
      });

    case "audit_entry": {
      const row = p as AuditRow;
      const trail = [...state.auditTrail, row].slice(-AUDIT_TRAIL_MAX);
      const next = { ...state, auditTrail: trail };
      return withOpenTurn(next, (t) => {
        const steps = t.steps.slice();
        const i = _lastOpen(steps);
        const hash = String(row.hash ?? "");
        if (i < 0) {
          // A denial with no preceding start/verdict — surface it as a step
          // rather than dropping it (the whole point of a governance view).
          return pushStep(t, {
            tool: String(row.tool ?? "?"),
            argsPreview: "",
            sentinelAllowed:
              typeof row.allowed === "boolean" ? row.allowed : undefined,
            sentinelReason: String(row.sentinel_reason ?? ""),
            isError: row.allowed === false,
            auditHash: hash,
          });
        }
        // Seal the open step. If it never got a verdict (envelope/plan
        // denial), reflect the audit row's allowed flag so the block shows.
        steps[i] = {
          ...steps[i],
          auditHash: hash,
          sentinelAllowed:
            steps[i].sentinelAllowed ??
            (typeof row.allowed === "boolean" ? row.allowed : undefined),
          sentinelReason:
            steps[i].sentinelReason ?? String(row.sentinel_reason ?? ""),
          isError: steps[i].isError ?? row.allowed === false,
        };
        return { ...t, steps };
      });
    }

    // Mid-turn steering: queued on POST .../steer, applied before the next model call.
    case "steer_queued":
      return withOpenTurn(state, (t) => ({
        ...t,
        items: [...t.items, { kind: "steer", text: String(p.text ?? ""), applied: false }],
      }));

    case "steer_injected":
      return withOpenTurn(state, (t) => {
        const i = t.items.findIndex(
          (it) => it.kind === "steer" && !it.applied && it.text === String(p.text ?? ""),
        );
        if (i < 0) return t;
        const items = t.items.slice();
        items[i] = { kind: "steer", text: String(p.text ?? ""), applied: true };
        return { ...t, items };
      });

    // Queued after the turn's last model call: the loop discarded it at turn end.
    case "steer_dropped":
      return withOpenTurn(state, (t) => ({
        ...t,
        items: t.items.map((it) =>
          it.kind === "steer" && !it.applied ? { ...it, dropped: true } : it),
      }));

    case "error":
      return withOpenTurn(state, (t) => ({
        ...t,
        errorText: String(p.error ?? "unknown error"),
      }));

    case "turn_done":
      return {
        ...withOpenTurn(state, (t) => ({
          ...t,
          done: true,
          haltedReason: String(p.halted_reason ?? ""),
          tokensIn: Number(p.tokens_in ?? 0),
          tokensOut: Number(p.tokens_out ?? 0),
          filesChanged: Array.isArray(p.files_changed) ? p.files_changed.map(String) : [],
        })),
        turnInFlight: false,
        context: p.num_ctx
          ? { used: Number(p.prompt_tokens ?? 0), max: Number(p.num_ctx) }
          : state.context,
      };

    case "permission_request": {
      const req = p as unknown as PendingPermission;
      if (state.pending.some((r) => r.req_id === req.req_id)) return state;
      return { ...state, pending: [...state.pending, req] };
    }

    case "permission_resolved":
    case "permission_timeout":
      return {
        ...state,
        pending: state.pending.filter((r) => r.req_id !== p.req_id),
        reviewWrites: typeof p.review_writes === "boolean" ? p.review_writes : state.reviewWrites,
      };

    case "file_reverted": {
      const path = String(p.path ?? "");
      const target = p.turn == null ? null : Number(p.turn);
      const thread = state.thread.map((t) =>
        target === null || t.turn === target
          ? { ...t, revertedFiles: [...(t.revertedFiles ?? []), path] }
          : t,
      );
      return { ...state, thread };
    }

    // A rewind drops the undone turns from the thread.
    case "rewind":
      if (typeof p.user_turn !== "number") return state;
      return { ...state, thread: state.thread.filter((t) => t.turn <= Number(p.user_turn)) };

    case "envelope_revoked":
      return { ...state, revoked: true };

    case "todo_update":
      return { ...state, todos: Array.isArray(p.items) ? (p.items as TodoItem[]) : [] };

    case "plan_saved":
    case "plan_updated":
      return {
        ...state,
        // A freshly drafted plan starts a new checklist; an edit keeps it.
        todos: ev.method === "plan_saved" ? [] : state.todos,
        spec: { version: state.spec.version + 1, bytes: Number(p.bytes ?? 0), error: null },
      };

    case "plan_failed":
      return { ...state, spec: { ...state.spec, error: String(p.reason ?? "planning failed") } };

    case "autonomy_change":
      return { ...state, autonomy: String(p.to ?? state.autonomy ?? "") || null };

    case "git_commit":
      return {
        ...state,
        lastCommit: {
          sha: String(p.sha ?? ""),
          paths: Array.isArray(p.paths) ? p.paths.map(String) : [],
          message_first_line: String(p.message_first_line ?? ""),
        },
      };

    case "session_resumed":
      return {
        ...state,
        resumed: { sessionId: String(p.session_id ?? ""), turns: Number(p.user_turns ?? 0) },
      };

    case "memory_proposed":
    case "memory_decided":
    case "note_pinned":
    case "brain_primed":
    case "skills_suggested":
      return reduceMemory(state, ev);

    default:
      return state;
  }
}

// The second-brain events: proposals queue and clear, pins toggle.
export function reduceMemory(state: StreamState, ev: StreamEvent): StreamState {
  const p = ev.params;
  const id = String(p.id ?? "");
  switch (ev.method) {
    case "memory_proposed": {
      if (state.proposals.some((m) => m.id === id)) return state;
      const row: MemoryProposal = {
        id, name: String(p.name ?? ""), description: String(p.description ?? ""),
        type: String(p.type ?? ""),
      };
      return { ...state, proposals: [...state.proposals, row] };
    }
    case "memory_decided":
      return { ...state, proposals: state.proposals.filter((m) => m.id !== id) };
    case "note_pinned": {
      const path = String(p.path ?? "");
      const rest = state.pinned.filter((n) => n.path !== path);
      return {
        ...state,
        pinned: p.pinned ? [...rest, { path, tier: Number(p.tier ?? 0) }] : rest,
      };
    }
    case "brain_primed":
      return {
        ...state,
        recall: {
          paths: (p.paths as string[]) ?? [],
          tiers: (p.tiers as number[]) ?? [],
          bytes: Number(p.bytes ?? 0),
        },
      };
    case "skills_suggested":
      return { ...state, suggestedSkills: (p.names as string[]) ?? [] };
    default:
      return state;
  }
}

// Poll reconciliation: GET /permissions is the source of truth — a dropped
// SSE permission_request must surface, and a stale card must clear.
export function reconcilePending(
  state: StreamState,
  serverPending: PendingPermission[],
): StreamState {
  // Order-insensitive: the server may return the same set in a different
  // order, and churning a new array every 3s poll would re-render for nothing.
  const localIds = new Set(state.pending.map((r) => r.req_id));
  const serverIds = new Set(serverPending.map((r) => r.req_id));
  const same =
    localIds.size === serverIds.size &&
    [...serverIds].every((id) => localIds.has(id));
  if (same) return state;
  return { ...state, pending: serverPending };
}
