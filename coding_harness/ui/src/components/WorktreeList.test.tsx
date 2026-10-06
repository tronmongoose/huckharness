import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import type { WorktreeRow } from "@/lib/types";
import { validTopic, WorktreeList } from "./WorktreeList";

describe("validTopic", () => {
  it("matches the server's rule", () => {
    expect(validTopic("feat-1.x")).toBe(true);
    for (const bad of ["", ".hidden", "-x", "a..b", "x.lock", "a b", "a/b", "x".repeat(65)]) {
      expect(validTopic(bad)).toBe(false);
    }
  });
});

afterEach(() => vi.unstubAllGlobals());

const row = (over: Partial<WorktreeRow>): WorktreeRow => ({
  path: "/p/repo", branch: "main", head: "a", detached: false, bare: false, locked: false,
  is_main: true, live: null, current: true, ...over,
});

const rows = [
  row({}),
  row({ path: "/p/repo-feat", branch: "feat", is_main: false, current: false, live: "http://127.0.0.1:7" }),
];

describe("WorktreeList", () => {
  it("opens only the listed path in a new tab", async () => {
    const posts = routeFetch({ "/v1/worktrees/open": { origin: "http://127.0.0.1:7" },
      "/v1/worktrees": { worktrees: rows } });
    const opened = vi.fn();
    vi.stubGlobal("open", opened);
    render(<WorktreeList />);
    expect(await screen.findByText("repo-feat")).toBeInTheDocument();
    expect(screen.getByText("here")).toBeInTheDocument();
    expect(screen.getAllByText("open")).toHaveLength(1);
    fireEvent.click(screen.getByText("open"));
    await vi.waitFor(() => expect(opened).toHaveBeenCalled());
    expect(posts).toEqual([{ url: expect.stringContaining("/v1/worktrees/open"), body: { path: "/p/repo-feat" } }]);
    expect(opened.mock.calls[0][0]).toBe("http://127.0.0.1:7/?new=1");
  });

  it("creates a worktree only for a valid topic", async () => {
    const posts = routeFetch({ "/v1/worktrees": { worktrees: rows, path: "/p/repo-x" } });
    render(<WorktreeList />);
    await screen.findByText("repo-feat");
    const button = screen.getByRole("button", { name: "new worktree" });
    fireEvent.change(screen.getByLabelText("new worktree topic"), { target: { value: "bad topic" } });
    expect(button).toBeDisabled();
    fireEvent.change(screen.getByLabelText("new worktree topic"), { target: { value: "x" } });
    fireEvent.click(button);
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].body).toEqual({ topic: "x" });
  });
});
