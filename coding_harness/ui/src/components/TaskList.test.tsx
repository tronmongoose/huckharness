import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { TodoItem } from "@/lib/types";
import { TaskList, taskSummary } from "./TaskList";

const ITEMS: TodoItem[] = [
  { id: "1", text: "read the code", status: "done" },
  { id: "2", text: "edit it", status: "in_progress" },
  { id: "3", text: "run tests", status: "pending" },
];

describe("TaskList", () => {
  it("renders nothing for an empty list", () => {
    const { container } = render(<TaskList items={[]} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("marks each status and flags the running step", () => {
    render(<TaskList items={ITEMS} />);
    expect(screen.getByText("tasks · 1 of 3 done")).toBeInTheDocument();
    expect(screen.getByText("[x]")).toBeInTheDocument();
    expect(screen.getByText("[ ]")).toBeInTheDocument();
    const running = screen.getByText("edit it").closest("li");
    expect(running).toHaveAttribute("aria-current", "step");
  });

  it("summarizes progress", () => {
    expect(taskSummary(ITEMS)).toBe("1 of 3 done");
  });
});
