import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { WorkView } from "./WorkView";

afterEach(() => vi.unstubAllGlobals());

const WORK = {
  items: [
    { id: "sl-a1", title: "fix the gate", status: "open", priority: 0, issue_type: "bug" },
    { id: "sl-b2", title: "write docs", status: "open", priority: 2, issue_type: "task" },
  ],
  status: "open",
  source: "live",
  writable: true,
  dir: "/t",
};

const DETAIL = {
  id: "sl-a1", title: "fix the gate", status: "open", priority: 0, issue_type: "bug",
  description: "the gate leaks", notes: "seen twice", acceptance_criteria: null, design: null,
  owner: null, labels: [], parent: null, close_reason: null, created_at: null, closed_at: null,
  dependencies: [{ id: "sl-c3", title: "upstream", status: "open", priority: 1,
    issue_type: "task", dependency_type: "blocks" }],
  dependents: [],
};

function stub(work: object = WORK) {
  const posts: { url: string; body: unknown }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ url: String(url), body: JSON.parse(String(init.body)) });
      return new Response(JSON.stringify({ ok: true, id: "sl-new" }));
    }
    if (String(url).includes("/v1/beads/")) return new Response(JSON.stringify(DETAIL));
    return new Response(JSON.stringify(work));
  }));
  return posts;
}

async function openDrawer() {
  fireEvent.click(await screen.findByText("fix the gate"));
  return screen.findByRole("dialog", { name: "bead sl-a1" });
}

describe("WorkView", () => {
  it("groups by priority and filters by type and text", async () => {
    stub();
    render(<WorkView />);
    expect(await screen.findByText(/P0 — critical · 1/)).toBeTruthy();
    expect(screen.getByText(/P2 — next · 1/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("type filter"), { target: { value: "task" } });
    expect(screen.queryByText("fix the gate")).toBeNull();
    fireEvent.change(screen.getByLabelText("type filter"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("search work"), { target: { value: "gate" } });
    expect(screen.queryByText("write docs")).toBeNull();
    expect(screen.getByText("fix the gate")).toBeTruthy();
  });

  it("shows the server's reason when there is no tracker", async () => {
    stub({ items: [], status: "open", source: "none", writable: false,
      reason: "no beads here; set beads_dir in ~/.config/bjorn/settings.json" });
    render(<WorkView />);
    expect(await screen.findByText(/set beads_dir/)).toBeTruthy();
    expect(screen.queryByLabelText("new bead title")).toBeNull();
  });

  it("opens the drawer on a row click with the bead's detail", async () => {
    stub();
    render(<WorkView />);
    await openDrawer();
    expect(await screen.findByText("the gate leaks")).toBeTruthy();
    expect(screen.getByText("seen twice")).toBeTruthy();
    expect(screen.getByText("upstream")).toBeTruthy();
  });

  it("claims and notes with the right bodies", async () => {
    const posts = stub();
    render(<WorkView />);
    await openDrawer();
    fireEvent.click(await screen.findByText("claim"));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ url: expect.stringMatching(/\/v1\/beads\/sl-a1\/claim$/), body: {} });
    await vi.waitFor(() => expect((screen.getByText("claim") as HTMLButtonElement).disabled).toBe(false));
    fireEvent.change(screen.getByLabelText("note"), { target: { value: " try again " } });
    fireEvent.click(screen.getByText("add note"));
    await vi.waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1]).toEqual({ url: expect.stringMatching(/\/v1\/beads\/sl-a1\/note$/), body: { text: "try again" } });
  });

  it("closes only after confirm with a reason", async () => {
    const posts = stub();
    render(<WorkView />);
    await openDrawer();
    fireEvent.click(await screen.findByText("close"));
    const confirm = screen.getByText("confirm close") as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
    fireEvent.click(confirm);
    expect(posts).toHaveLength(0);
    fireEvent.change(screen.getByLabelText("close reason"), { target: { value: "shipped" } });
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ url: expect.stringMatching(/\/v1\/beads\/sl-a1\/close$/), body: { reason: "shipped" } });
  });

  it("files a new bead with title, priority and type", async () => {
    const posts = stub();
    render(<WorkView />);
    fireEvent.change(await screen.findByLabelText("new bead title"), { target: { value: "new thing" } });
    fireEvent.change(screen.getByLabelText("new bead priority"), { target: { value: "1" } });
    fireEvent.change(screen.getByLabelText("new bead type"), { target: { value: "bug" } });
    fireEvent.click(screen.getByText("file"));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ url: expect.stringMatching(/\/v1\/beads$/),
      body: { title: "new thing", priority: 1, type: "bug" } });
  });

  it("hides actions when the server is read-only", async () => {
    stub({ ...WORK, writable: false });
    render(<WorkView />);
    await openDrawer();
    expect(await screen.findByText(/read-only: writes are off/)).toBeTruthy();
    expect(screen.queryByText("claim")).toBeNull();
  });
});
