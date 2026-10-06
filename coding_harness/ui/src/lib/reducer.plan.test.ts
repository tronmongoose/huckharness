import { describe, expect, it } from "vitest";

import { initialStreamState, reduceEvent, type StreamState } from "./reducer";
import type { StreamEvent } from "./types";

function run(events: StreamEvent[]): StreamState {
  return events.reduce(reduceEvent, initialStreamState);
}

describe("spec-then-run events", () => {
  it("replaces the task list on each todo_update", () => {
    const s = run([
      { method: "todo_update", params: { items: [{ id: "1", text: "a", status: "pending" }] } },
      { method: "todo_update", params: { items: [{ id: "1", text: "a", status: "done" }] } },
    ]);
    expect(s.todos).toEqual([{ id: "1", text: "a", status: "done" }]);
  });

  it("bumps the plan version on save and edit, keeps the failure reason", () => {
    const s = run([
      { method: "plan_failed", params: { reason: "no plan text" } },
      { method: "plan_saved", params: { path: "/x.md", bytes: 12 } },
      { method: "plan_updated", params: { bytes: 20 } },
    ]);
    expect(s.spec).toEqual({ version: 2, bytes: 20, error: null });
    expect(run([{ method: "plan_failed", params: { reason: "boom" } }]).spec.error).toBe("boom");
  });

  it("clears the checklist when a new plan is saved, not on an edit", () => {
    const todo = { method: "todo_update", params: { items: [{ id: "1", text: "a", status: "done" }] } };
    expect(run([todo, { method: "plan_updated", params: { bytes: 1 } }]).todos).toHaveLength(1);
    expect(run([todo, { method: "plan_saved", params: { bytes: 1 } }]).todos).toEqual([]);
  });

  it("tracks the autonomy level", () => {
    const s = run([{ method: "autonomy_change", params: { from: "off", to: "medium" } }]);
    expect(s.autonomy).toBe("medium");
  });
});
