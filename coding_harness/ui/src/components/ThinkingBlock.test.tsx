import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SubagentCard } from "./SubagentCard";
import { ThinkingBlock } from "./ThinkingBlock";

afterEach(() => vi.unstubAllGlobals());

function motion(reduce: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: reduce && q.includes("reduce"),
    addEventListener() {}, removeEventListener() {} }));
}

describe("ThinkingBlock", () => {
  it("names the running tool and its argument", () => {
    motion(false);
    render(<ThinkingBlock phase={{ kind: "tool", since: Date.now(), detail: "Read", tokens: 0 }}
      turn={1} summary="core/session.py" />);
    expect(screen.getByText("Reading core/session.py…")).toBeInTheDocument();
    expect(screen.getByText(/running Read/)).toBeInTheDocument();
  });

  it("holds a static glyph when motion is reduced", () => {
    motion(true);
    render(<ThinkingBlock phase={{ kind: "starting", since: Date.now(), detail: "", tokens: 0 }} turn={1} />);
    expect(screen.getByText("◉")).toBeInTheDocument();
  });
});

describe("SubagentCard", () => {
  const agent = { id: "c1", agent: "explore", task: "find x", model: "granite4.1:8b",
    steps: [{ tool: "Grep", summary: "x" }], done: false };

  it("shows the live steps while the helper works", () => {
    render(<SubagentCard agent={agent} turnDone={false} />);
    expect(screen.getByText("working")).toBeInTheDocument();
    expect(screen.getByText("Grep")).toBeInTheDocument();
  });

  it("folds to one line once the turn is done", () => {
    render(<SubagentCard agent={{ ...agent, done: true, stepCount: 3, halted: "model_done" }} turnDone />);
    expect(screen.getByText("done in 3 steps")).toBeInTheDocument();
    expect(screen.queryByText("Grep")).toBeNull();
  });
});
