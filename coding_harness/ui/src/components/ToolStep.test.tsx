import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { ToolStep } from "@/lib/types";
import { StepView, stepSummary } from "./ToolStep";

const step = (over: Partial<ToolStep>): ToolStep => ({ tool: "Bash", argsPreview: "", ...over });

describe("StepView", () => {
  it("summarizes each tool by the argument that identifies the call", () => {
    expect(stepSummary(step({ args: { command: "make test" } }))).toBe("make test");
    expect(stepSummary(step({ tool: "Edit", args: { file_path: "a.py" } }))).toBe("a.py");
    expect(stepSummary(step({ tool: "Grep", args: { pattern: "x", path: "src" } }))).toBe(
      "x  in  src",
    );
  });

  it("summarizes a TodoWrite list by status counts", () => {
    const items = [
      { id: "1", text: "a", status: "done" },
      { id: "2", text: "b", status: "in_progress" },
      { id: "3", text: "c", status: "pending" },
      { id: "4", text: "d", status: "pending" },
    ];
    expect(stepSummary(step({ tool: "TodoWrite", args: { items } }))).toBe(
      "4 items: 1 done, 1 in progress, 2 pending",
    );
    expect(stepSummary(step({ tool: "TodoWrite", args: {} }))).toBe(
      "0 items: 0 done, 0 in progress, 0 pending",
    );
  });

  it("expands an Edit into removed and added lines", () => {
    render(
      <StepView
        step={step({ tool: "Edit", args: { file_path: "a.py", old_string: "old", new_string: "new" } })}
      />,
    );
    expect(screen.queryByText("- old")).toBeNull();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("- old")).toBeInTheDocument();
    expect(screen.getByText("+ new")).toBeInTheDocument();
  });

  it("shows the full output the model saw", () => {
    render(<StepView step={step({ args: { command: "ls" }, resultPreview: "a", result: "a\nb" })} />);
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText(/\$ ls/)).toBeInTheDocument();
    expect(screen.getByText((_, el) => el?.textContent === "a\nb")).toBeInTheDocument();
  });

  it("labels a blocked call with its reason", () => {
    render(<StepView step={step({ sentinelAllowed: false, sentinelReason: "rm -rf" })} />);
    expect(screen.getByText("blocked")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("blocked: rm -rf")).toBeInTheDocument();
  });
});
