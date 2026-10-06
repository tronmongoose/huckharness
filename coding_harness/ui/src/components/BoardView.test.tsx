import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BoardAll, BoardRow } from "@/lib/types";
import { BoardView, sessionUrl } from "./BoardView";
import { elapsed } from "./SessionCard";

afterEach(() => vi.unstubAllGlobals());

const row = (id: string, over: Partial<BoardRow> = {}): BoardRow => ({
  session_id: id, title: `task ${id}`, status: "idle", pending: 0, last_excerpt: "",
  last_event_ts: null, turn: 1, model: "m1", autonomy: "low", ...over,
});

const board: BoardAll = {
  servers: [
    {
      server: { cwd: "/home/u/projects/alpha", port: 1, pid: 1 }, origin: "http://127.0.0.1:1", self: true,
      sessions: [
        row("a1", { status: "running", last_excerpt: "reading files" }),
        row("a2", { status: "needs_approval", pending: 2 }),
      ],
    },
    {
      server: { cwd: "/home/u/projects/beta", port: 2, pid: 2 }, origin: "http://127.0.0.1:2",
      sessions: [row("b1", { status: "error" }), row("b2", { status: "done" })],
    },
  ],
  errors: [{ origin: "http://127.0.0.1:3", error: "URLError" }],
};

describe("BoardView", () => {
  it("renders one card per session with its project and status", () => {
    render(<BoardView board={board} onSelect={vi.fn()} />);
    expect(screen.getAllByRole("button")).toHaveLength(4);
    expect(screen.getAllByText("alpha")).toHaveLength(2);
    expect(screen.getAllByText("beta")).toHaveLength(2);
    expect(screen.getByText("running")).toBeInTheDocument();
    expect(screen.getByText("needs approval · 2")).toBeInTheDocument();
    expect(screen.getByText("error")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getByText("reading files")).toBeInTheDocument();
    expect(screen.getByText(/unreachable: http:\/\/127.0.0.1:3/)).toBeInTheDocument();
  });

  it("selects a local session and opens a remote one in its own server", () => {
    const onSelect = vi.fn();
    const open = vi.fn();
    vi.stubGlobal("open", open);
    render(<BoardView board={board} onSelect={onSelect} />);
    fireEvent.click(screen.getByRole("button", { name: /alpha task a1/ }));
    expect(onSelect).toHaveBeenCalledWith("a1");
    fireEvent.click(screen.getByRole("button", { name: /beta task b1/ }));
    expect(open).toHaveBeenCalledWith("http://127.0.0.1:2/#s=b1", "_blank", "noopener");
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("shows an empty state with no sessions", () => {
    render(<BoardView board={{ servers: [], errors: [] }} onSelect={vi.fn()} />);
    expect(screen.getByText(/No sessions/)).toBeInTheDocument();
  });

  it("formats elapsed time and session urls", () => {
    expect(elapsed(null, 0)).toBe("");
    expect(elapsed(100, 130_000)).toBe("30s");
    expect(elapsed(100, 400_000)).toBe("5m");
    expect(sessionUrl("http://127.0.0.1:9", "x y")).toBe("http://127.0.0.1:9/#s=x%20y");
  });
});
