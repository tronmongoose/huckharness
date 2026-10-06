import { describe, expect, it } from "vitest";

import {
  initialStreamState,
  reconcilePending,
  reduceEvent,
  type StreamState,
} from "./reducer";
import type { PendingPermission, StreamEvent } from "./types";

function run(events: StreamEvent[], from = initialStreamState): StreamState {
  return events.reduce(reduceEvent, from);
}

const req = (id: string): PendingPermission => ({
  req_id: id,
  tool: "Write",
  args_preview: { file_path: "/etc/x" },
  reason: "out_of_envelope:Write:write:/etc/x",
  created_at: new Date().toISOString(),
});

describe("reduceEvent", () => {
  it("accumulates assistant deltas into the open turn", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "assistant_delta", params: { text: "hel" } },
      { method: "assistant_delta", params: { text: "lo" } },
    ]);
    expect(s.thread).toHaveLength(1);
    expect(s.thread[0].streamingText).toBe("hello");
    expect(s.turnInFlight).toBe(true);
  });

  it("seals the turn on turn_done with telemetry", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "assistant_delta", params: { text: "done" } },
      {
        method: "turn_done",
        params: { halted_reason: "model_done", tokens_in: 10, tokens_out: 5 },
      },
    ]);
    expect(s.thread[0].done).toBe(true);
    expect(s.thread[0].haltedReason).toBe("model_done");
    expect(s.thread[0].tokensOut).toBe(5);
    expect(s.turnInFlight).toBe(false);
  });

  it("builds a tool step in real emit order: verdict, start, result, audit", () => {
    // registry emits sentinel_verdict BEFORE tool_call_start.
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      {
        method: "sentinel_verdict",
        params: { tool: "Read", allowed: true, reason: "ok" },
      },
      {
        method: "tool_call_start",
        params: { tool: "Read", args: { file_path: "/x" } },
      },
      {
        method: "tool_call_result",
        params: { tool: "Read", is_error: false, content_preview: "data" },
      },
      {
        method: "audit_entry",
        params: { tool: "Read", allowed: true, hash: "abcdef1234567890" },
      },
    ]);
    expect(s.thread[0].steps).toHaveLength(1);
    const step = s.thread[0].steps[0];
    expect(step.tool).toBe("Read");
    expect(step.sentinelAllowed).toBe(true);
    expect(step.argsPreview).toContain("/x");
    expect(step.resultPreview).toBe("data");
    expect(step.auditHash).toBe("abcdef1234567890");
    expect(s.auditTrail).toHaveLength(1);
  });

  it("keeps two consecutive same-tool calls on separate steps", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "sentinel_verdict", params: { tool: "Read", allowed: true, reason: "ok" } },
      { method: "tool_call_start", params: { tool: "Read", args: { file_path: "/a" } } },
      { method: "tool_call_result", params: { tool: "Read", content_preview: "A" } },
      { method: "audit_entry", params: { tool: "Read", allowed: true, hash: "h1aaaaaa" } },
      { method: "sentinel_verdict", params: { tool: "Read", allowed: true, reason: "ok" } },
      { method: "tool_call_start", params: { tool: "Read", args: { file_path: "/b" } } },
      { method: "tool_call_result", params: { tool: "Read", content_preview: "B" } },
      { method: "audit_entry", params: { tool: "Read", allowed: true, hash: "h2bbbbbb" } },
    ]);
    expect(s.thread[0].steps).toHaveLength(2);
    expect(s.thread[0].steps[0].resultPreview).toBe("A");
    expect(s.thread[0].steps[0].auditHash).toBe("h1aaaaaa");
    expect(s.thread[0].steps[1].resultPreview).toBe("B");
    expect(s.thread[0].steps[1].auditHash).toBe("h2bbbbbb");
  });

  it("surfaces an envelope-denied call (audit_entry only, no start)", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      {
        method: "audit_entry",
        params: {
          tool: "Write", allowed: false,
          sentinel_reason: "envelope_denied", hash: "deadbeef00",
        },
      },
    ]);
    expect(s.thread[0].steps).toHaveLength(1);
    const step = s.thread[0].steps[0];
    expect(step.tool).toBe("Write");
    expect(step.sentinelAllowed).toBe(false);
    expect(step.isError).toBe(true);
    expect(step.auditHash).toBe("deadbeef00");
  });

  it("captures a model error onto the open turn", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "error", params: { error: "ollama transport error" } },
      { method: "turn_done", params: { halted_reason: "error" } },
    ]);
    expect(s.thread[0].errorText).toBe("ollama transport error");
    expect(s.thread[0].haltedReason).toBe("error");
    expect(s.turnInFlight).toBe(false);
  });

  it("adds and resolves pending permissions, deduped", () => {
    const add: StreamEvent = {
      method: "permission_request",
      params: req("r1") as unknown as Record<string, unknown>,
    };
    let s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      add,
      add,
    ]);
    expect(s.pending).toHaveLength(1);
    s = reduceEvent(s, {
      method: "permission_resolved",
      params: { req_id: "r1", decision: "allow_once" },
    });
    expect(s.pending).toHaveLength(0);
  });

  it("clears pending on timeout and marks revoked", () => {
    let s = run([
      {
        method: "permission_request",
        params: req("r2") as unknown as Record<string, unknown>,
      },
    ]);
    s = reduceEvent(s, {
      method: "permission_timeout",
      params: { req_id: "r2" },
    });
    expect(s.pending).toHaveLength(0);
    s = reduceEvent(s, { method: "envelope_revoked", params: {} });
    expect(s.revoked).toBe(true);
  });

  it("captures identity from session_start", () => {
    const s = run([
      {
        method: "session_start",
        params: { identity: "carryall:startup-agent#abc" },
      },
    ]);
    expect(s.identity).toBe("carryall:startup-agent#abc");
  });

  it("flags a thread rebuilt by session_resumed", () => {
    expect(initialStreamState.resumed).toBeNull();
    const s = run([{ method: "session_resumed",
      params: { session_id: "20260102T030405-ab", messages: 9, user_turns: 4 } }]);
    expect(s.resumed).toEqual({ sessionId: "20260102T030405-ab", turns: 4 });
  });
});

describe("reconcilePending", () => {
  it("replaces local pending with the server truth", () => {
    const local = run([
      {
        method: "permission_request",
        params: req("stale") as unknown as Record<string, unknown>,
      },
    ]);
    const server = [req("fresh")];
    const s = reconcilePending(local, server);
    expect(s.pending.map((r) => r.req_id)).toEqual(["fresh"]);
  });

  it("is a no-op when ids match (referential stability)", () => {
    const local = run([
      {
        method: "permission_request",
        params: req("same") as unknown as Record<string, unknown>,
      },
    ]);
    const s = reconcilePending(local, [local.pending[0]]);
    expect(s).toBe(local);
  });
});

describe("thread order", () => {
  it("interleaves prose and tool steps as they happened", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "assistant_delta", params: { text: "look" } },
      { method: "assistant_delta", params: { text: "ing" } },
      { method: "sentinel_verdict", params: { tool: "Bash", allowed: true } },
      { method: "tool_call_start", params: { tool: "Bash", args: { command: "ls" } } },
      { method: "tool_call_result", params: { content_preview: "a", content: "a\nb" } },
      { method: "audit_entry", params: { tool: "Bash", hash: "h1", allowed: true } },
      { method: "assistant_delta", params: { text: "done" } },
    ]);
    const t = s.thread[0];
    expect(t.items).toEqual([
      { kind: "text", text: "looking" },
      { kind: "step", index: 0 },
      { kind: "text", text: "done" },
    ]);
    expect(t.steps[0].args).toEqual({ command: "ls" });
    expect(t.steps[0].result).toBe("a\nb");
  });

  it("gives a bare audit denial its own place in the order", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "audit_entry", params: { tool: "Write", hash: "h", allowed: false } },
    ]);
    expect(s.thread[0].items).toEqual([{ kind: "step", index: 0 }]);
    expect(s.thread[0].steps[0].sentinelAllowed).toBe(false);
  });
});

describe("turn phase", () => {
  const kinds = (events: StreamEvent[]) => {
    const out: string[] = [];
    events.reduce((s, ev) => {
      const next = reduceEvent(s, ev);
      out.push(next.phase.kind);
      return next;
    }, initialStreamState);
    return out;
  };

  it("walks baseline, wait, tool, generation, then idle", () => {
    expect(
      kinds([
        { method: "turn_start", params: { turn: 1, prompt: "go" } },
        { method: "model_call_start", params: { model: "phi4-mini", step: 1 } },
        { method: "checks_baseline_start", params: { checks: ["make test"] } },
        { method: "checks_baseline", params: { failures: 0 } },
        { method: "tool_call_start", params: { tool: "Edit", args: {} } },
        { method: "model_call_start", params: { model: "phi4-mini", step: 2 } },
        { method: "assistant_delta", params: { text: "a" } },
        { method: "assistant_delta", params: { text: "b" } },
        { method: "turn_done", params: {} },
      ]),
    ).toEqual(["starting", "waiting", "baseline", "baseline", "tool", "waiting",
      "generating", "generating", "idle"]);
  });

  it("counts streamed deltas and keeps the model name while generating", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "model_call_start", params: { model: "phi4-mini", step: 1 } },
      { method: "assistant_delta", params: { text: "a" } },
      { method: "assistant_delta", params: { text: "b" } },
    ]);
    expect(s.phase).toMatchObject({ kind: "generating", detail: "phi4-mini", tokens: 2 });
  });
});

describe("route", () => {
  it("records the model that answered the turn", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "route_decision", params: { model: "gpt-oss:20b", route_reason: "override_explicit_model" } },
    ]);
    expect(s.thread[0]).toMatchObject({ model: "gpt-oss:20b", routeReason: "override_explicit_model" });
  });
});

describe("context use", () => {
  it("takes the window from turn_done and keeps it across turns without one", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "turn_done", params: { prompt_tokens: 9000, num_ctx: 32768 } },
      { method: "turn_start", params: { turn: 2, prompt: "again" } },
      { method: "turn_done", params: {} },
    ]);
    expect(s.context).toEqual({ used: 9000, max: 32768 });
  });
});

describe("changes events", () => {
  const twoTurns: StreamEvent[] = [
    { method: "turn_start", params: { turn: 1, prompt: "a", checkpoint: "sha1" } },
    { method: "turn_done", params: { files_changed: ["/w/x.txt", "/w/y.txt"] } },
    { method: "turn_start", params: { turn: 2, prompt: "b", checkpoint: null } },
    { method: "turn_done", params: { files_changed: [] } },
  ];

  it("keeps the checkpoint and files_changed on each turn", () => {
    const s = run(twoTurns);
    expect(s.thread[0].checkpoint).toBe("sha1");
    expect(s.thread[0].filesChanged).toEqual(["/w/x.txt", "/w/y.txt"]);
    expect(s.thread[1].checkpoint).toBeNull();
    expect(s.thread[1].filesChanged).toEqual([]);
  });

  it("marks file_reverted on the matching turn only", () => {
    const s = run([...twoTurns, { method: "file_reverted", params: { path: "x.txt", turn: 1 } }]);
    expect(s.thread[0].revertedFiles).toEqual(["x.txt"]);
    expect(s.thread[1].revertedFiles).toBeUndefined();
  });

  it("drops rewound turns from the thread", () => {
    const s = run([...twoTurns, { method: "rewind", params: { user_turn: 1 } }]);
    expect(s.thread.map((t) => t.turn)).toEqual([1]);
  });

  it("takes review_writes from a resolved review request", () => {
    const s = run([
      { method: "permission_request", params: { ...req("r1"), kind: "review" } },
      { method: "permission_resolved", params: { req_id: "r1", decision: "allow_always", kind: "review", review_writes: false } },
    ]);
    expect(s.reviewWrites).toBe(false);
    expect(s.pending).toEqual([]);
    const plain = run([{ method: "permission_resolved", params: { req_id: "x", decision: "deny" } }]);
    expect(plain.reviewWrites).toBeNull();
  });

  it("carries the kind of a pending request", () => {
    const s = run([
      { method: "permission_request", params: { ...req("r1"), kind: "review", args_preview: { diff: "+x" } } },
    ]);
    expect(s.pending[0].kind).toBe("review");
    expect(s.pending[0].args_preview.diff).toBe("+x");
  });
});

describe("subagents", () => {
  it("attaches a live helper to its Explore step and records its steps and end", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "tool_call_start", params: { tool: "Explore", args: { task: "find x" } } },
      { method: "subagent_start", params: { id: "c1", agent: "explore", task: "find x", model: "granite4.1:8b" } },
      { method: "subagent_step", params: { id: "c1", tool: "Grep", summary: "x" } },
      { method: "subagent_step", params: { id: "c1", tool: "Read", summary: "mod.py" } },
      { method: "subagent_done", params: { id: "c1", steps: 3, halted: "model_done" } },
    ]);
    const t = s.thread[0];
    expect(t.steps[0].subagentId).toBe("c1");
    expect(t.subagents?.c1).toMatchObject({ model: "granite4.1:8b", done: true, stepCount: 3,
      steps: [{ tool: "Grep", summary: "x" }, { tool: "Read", summary: "mod.py" }] });
  });

  it("ignores steps for a helper it never saw start", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "subagent_step", params: { id: "ghost", tool: "Grep", summary: "x" } },
    ]);
    expect(s.thread[0].subagents).toBeUndefined();
  });
});
