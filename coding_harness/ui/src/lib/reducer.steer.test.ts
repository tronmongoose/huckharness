import { describe, expect, it } from "vitest";

import { initialStreamState, reduceEvent, type StreamState } from "./reducer";
import type { StreamEvent } from "./types";

function run(events: StreamEvent[]): StreamState {
  return events.reduce(reduceEvent, initialStreamState);
}

describe("steer", () => {
  it("adds a queued steer item to the open turn, then marks it applied", () => {
    const queued = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "assistant_delta", params: { text: "working" } },
      { method: "steer_queued", params: { turn: 1, text: "use the small fix" } },
    ]);
    expect(queued.thread[0].items).toEqual([
      { kind: "text", text: "working" },
      { kind: "steer", text: "use the small fix", applied: false },
    ]);
    const applied = reduceEvent(queued, {
      method: "steer_injected", params: { turn: 1, step: 2, text: "use the small fix" },
    });
    expect(applied.thread[0].items[1]).toEqual({ kind: "steer", text: "use the small fix", applied: true });
  });

  it("marks an unapplied steer dropped when the turn discards it", () => {
    const s = run([
      { method: "turn_start", params: { turn: 1, prompt: "go" } },
      { method: "steer_queued", params: { turn: 1, text: "late" } },
      { method: "steer_dropped", params: { turn: 1, texts: ["late"] } },
    ]);
    expect(s.thread[0].items).toEqual([{ kind: "steer", text: "late", applied: false, dropped: true }]);
  });

  it("ignores a steer with no open turn", () => {
    const s = run([{ method: "steer_queued", params: { text: "late" } }]);
    expect(s.thread).toEqual([]);
  });
});
