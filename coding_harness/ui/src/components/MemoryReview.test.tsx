import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import { onUnauthorized } from "@/lib/api";
import { decideMemory, MemoryReview } from "./MemoryReview";
import { PinnedNotes } from "./PinnedNotes";

afterEach(() => vi.unstubAllGlobals());

const ROW = { id: "1", name: "per-file-tests", description: "Run tests one file at a time", type: "feedback" };
const TEXT = "---\nname: per-file-tests\n---\n\nbody";
const SHA = "a".repeat(64);

function setup() {
  const posts = routeFetch({
    "/v1/sessions/s1/memories": { proposals: [{ ...ROW, text: TEXT, sha256: SHA }], pinned: [] },
  });
  render(<MemoryReview sessionId="s1" proposals={[ROW]} />);
  return posts;
}

describe("MemoryReview", () => {
  it("shows the empty state with no proposals", () => {
    routeFetch({ "/v1/sessions/s1/memories": { proposals: [], pinned: [] } });
    render(<MemoryReview sessionId="s1" proposals={[]} />);
    expect(screen.getByText(/No proposals/)).toBeInTheDocument();
  });

  it("approves with the proposal id", async () => {
    const posts = setup();
    expect(await screen.findByText(/body/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("approve"));
    await waitFor(() => expect(posts).toEqual([
      { url: expect.stringContaining("/v1/sessions/s1/memories"), body: { id: "1", decision: "approve", sha256: SHA } },
    ]));
    await waitFor(() => expect(screen.queryByLabelText("proposal per-file-tests")).toBeNull());
  });

  it("rejects", async () => {
    const posts = setup();
    await screen.findByText(/body/);
    fireEvent.click(screen.getByText("reject"));
    await waitFor(() => expect(posts[0].body).toEqual({ id: "1", decision: "reject", sha256: SHA }));
  });

  it("edits inline and posts the replacement text", async () => {
    const posts = setup();
    await screen.findByText(/body/);
    fireEvent.click(screen.getByText("edit"));
    fireEvent.change(screen.getByLabelText("edit per-file-tests"), { target: { value: `${TEXT} more` } });
    fireEvent.click(screen.getByText("save and approve"));
    await waitFor(() => expect(posts[0].body).toEqual({ id: "1", decision: "edit", sha256: SHA, text: `${TEXT} more` }));
  });
});

describe("MemoryReview stale card", () => {
  it("shows the 409 and reloads the listing", async () => {
    const gets: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Response(JSON.stringify({ error: { code: 409, message: "the proposal changed" } }),
          { status: 409 });
      }
      gets.push(String(url));
      return new Response(JSON.stringify({ proposals: [{ ...ROW, text: TEXT, sha256: SHA }] }));
    }));
    render(<MemoryReview sessionId="s1" proposals={[ROW]} />);
    await screen.findByText(/body/);
    fireEvent.click(screen.getByText("approve"));
    expect(await screen.findByText("the proposal changed")).toBeInTheDocument();
    await waitFor(() => expect(gets.length).toBe(2));
    expect(screen.getByLabelText("proposal per-file-tests")).toBeInTheDocument();
  });

  it("keeps the buttons off until the text and its hash are loaded", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    render(<MemoryReview sessionId="s1" proposals={[ROW]} />);
    expect(screen.getByText("approve").closest("button")).toBeDisabled();
  });
});

describe("PinnedNotes", () => {
  it("unpins a note", async () => {
    const posts = routeFetch({ "/v1/sessions/s1/pin": { pinned: false } });
    render(<PinnedNotes sessionId="s1" pinned={[{ path: "finance/budget.md", tier: 2 }]}
      recall={{ paths: ["startup/plan.md"], tiers: [1], bytes: 120 }} />);
    expect(screen.getByText("confidential")).toBeInTheDocument();
    expect(screen.getByText(/last turn recalled 1 note/)).toBeInTheDocument();
    fireEvent.click(screen.getByTitle("unpin finance/budget.md"));
    await waitFor(() => expect(posts[0].body).toEqual({ path: "finance/budget.md", pinned: false }));
  });

  it("explains how to pin when empty", () => {
    render(<PinnedNotes sessionId="s1" pinned={[]} />);
    expect(screen.getByText(/Pin a note from the brain view/)).toBeInTheDocument();
  });
});

describe("decideMemory auth", () => {
  it("sends the session cookie and flips to login on a 401", async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ error: "unauthorized" }), { status: 401 }));
    vi.stubGlobal("fetch", fetchMock);
    const seen = vi.fn();
    const off = onUnauthorized(seen);
    const err = await decideMemory("s1", "1", "reject", SHA);
    off();
    expect(err).toBe("failed (401)");
    expect(seen).toHaveBeenCalledTimes(1);
    const init = (fetchMock.mock.calls[0] as unknown[])[1] as RequestInit;
    expect(init.credentials).toBe("same-origin");
  });
});
