import { describe, expect, it } from "vitest";

import { initialStreamState, reduceEvent } from "./reducer";

describe("git_commit", () => {
  it("records the panel's latest commit", () => {
    const s = reduceEvent(initialStreamState, {
      method: "git_commit",
      params: { sha: "abc", paths: ["a.py"], message_first_line: "gui: x" },
    });
    expect(s.lastCommit).toEqual({ sha: "abc", paths: ["a.py"], message_first_line: "gui: x" });
    expect(initialStreamState.lastCommit).toBeNull();
  });
});
