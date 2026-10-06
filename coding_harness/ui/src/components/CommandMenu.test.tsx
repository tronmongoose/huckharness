import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { type CommandRow, CommandMenu, filterCommands } from "./CommandMenu";

const row = (name: string, kind: CommandRow["kind"] = "builtin"): CommandRow =>
  ({ name, usage: name, help: `${name} help`, kind });

const ROWS = [row("/model"), row("/new"), row("/compact"), row("/stop"),
  row("/skill design-tenets", "skill"), row("/ship", "custom")];

describe("filterCommands", () => {
  it("puts prefix matches before loose subsequence matches", () => {
    expect(filterCommands(ROWS, "s").map((r) => r.name))
      .toEqual(["/stop", "/skill design-tenets", "/ship"]);
    expect(filterCommands(ROWS, "mt").map((r) => r.name)).toEqual(["/compact"]);
  });

  it("is case-insensitive and empty lists everything", () => {
    expect(filterCommands(ROWS, "MOD").map((r) => r.name)).toEqual(["/model"]);
    expect(filterCommands(ROWS, "")).toHaveLength(ROWS.length);
    expect(filterCommands(ROWS, "zz")).toEqual([]);
  });

  it("matches skill names after the space", () => {
    expect(filterCommands(ROWS, "skill des").map((r) => r.name)).toEqual(["/skill design-tenets"]);
  });
});

describe("CommandMenu", () => {
  it("marks the active row and reports a pick", () => {
    const onPick = vi.fn();
    render(<CommandMenu rows={ROWS.slice(0, 2)} active={1} onPick={onPick} />);
    expect(screen.getByRole("option", { name: /\/new/ })).toHaveAttribute("aria-selected", "true");
    fireEvent.mouseDown(screen.getByRole("option", { name: /\/model/ }));
    expect(onPick).toHaveBeenCalledWith(ROWS[0]);
  });

  it("renders nothing with no rows", () => {
    const { container } = render(<CommandMenu rows={[]} active={0} onPick={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });
});
