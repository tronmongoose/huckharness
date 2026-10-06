import { describe, expect, it } from "vitest";

import { initialStreamState, reduceEvent, type StreamState } from "./reducer";

function run(events: Array<[string, Record<string, unknown>]>): StreamState {
  return events.reduce(
    (s, [method, params]) => reduceEvent(s, { method, params }),
    initialStreamState,
  );
}

describe("memory reducer cases", () => {
  it("queues a proposal once and clears it on decision", () => {
    const row = { id: "1", name: "n", description: "d", type: "feedback" };
    const queued = run([["memory_proposed", row], ["memory_proposed", row]]);
    expect(queued.proposals).toEqual([row]);
    expect(reduceEvent(queued, { method: "memory_decided", params: { id: "1", decision: "reject" } })
      .proposals).toEqual([]);
  });

  it("pins, re-pins without duplicates, and unpins", () => {
    const pinned = run([
      ["note_pinned", { path: "a.md", pinned: true, tier: 2 }],
      ["note_pinned", { path: "a.md", pinned: true, tier: 2 }],
      ["note_pinned", { path: "b.md", pinned: true, tier: 1 }],
    ]);
    expect(pinned.pinned).toEqual([{ path: "a.md", tier: 2 }, { path: "b.md", tier: 1 }]);
    expect(reduceEvent(pinned, { method: "note_pinned", params: { path: "a.md", pinned: false } })
      .pinned).toEqual([{ path: "b.md", tier: 1 }]);
  });

  it("records the recall and the suggested skills", () => {
    const s = run([
      ["brain_primed", { turn: 1, paths: ["x.md"], tiers: [1], bytes: 90 }],
      ["skills_suggested", { names: ["code-review"] }],
    ]);
    expect(s.recall).toEqual({ paths: ["x.md"], tiers: [1], bytes: 90 });
    expect(s.suggestedSkills).toEqual(["code-review"]);
  });
});
