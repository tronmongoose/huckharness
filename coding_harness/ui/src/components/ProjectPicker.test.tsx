import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ProjectPicker } from "./ProjectPicker";

afterEach(() => vi.unstubAllGlobals());

const projects = [
  { name: "bjorn-harness", path: "/p/bjorn-harness", current: true, url: null },
  { name: "beta", path: "/p/beta", current: false, url: "http://127.0.0.1:5001" },
];

describe("ProjectPicker", () => {
  it("lists repos, marks running ones, and asks the server to open the pick", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (_u: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        posts.push(JSON.parse(String(init.body)));
        return new Response("null", { status: 500 });
      }
      return new Response(JSON.stringify({ projects }), { status: 200 });
    }));
    render(<ProjectPicker cwd="/p/bjorn-harness" />);
    expect(await screen.findByRole("option", { name: "beta · running" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("project"), { target: { value: "/p/beta" } });
    await vi.waitFor(() => expect(posts).toEqual([{ path: "/p/beta" }]));
    expect(await screen.findByText("could not start it")).toBeInTheDocument();
  });

  it("still names an unlisted cwd such as a worktree", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      new Response(JSON.stringify({ projects: [{ ...projects[1], current: false }] }), { status: 200 })));
    render(<ProjectPicker cwd="/p/bjorn-harness-gui-status" />);
    expect(await screen.findByRole("option", { name: "bjorn-harness-gui-status" })).toBeInTheDocument();
  });
});
