import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import type { TurnBlock } from "@/lib/types";
import { ChangesPanel } from "./ChangesPanel";

const block = (turn: number, done = true): TurnBlock => ({
  turn, prompt: "p", streamingText: "", steps: [], items: [], done, filesChanged: ["/w/a.txt"],
});

const diff = {
  turn: 2, base: "abc",
  files: [{ path: "a.txt", status: "added", diff: "--- /dev/null\n+++ b/a.txt\n@@ -0,0 +1 @@\n+hi", truncated: false }],
};

afterEach(() => vi.unstubAllGlobals());

describe("ChangesPanel", () => {
  it("fetches the latest finished turn's diff", async () => {
    routeFetch({ "/diff": diff });
    render(<ChangesPanel sessionId="s1" turns={[block(1), block(2)]} turnInFlight={false} />);
    expect(await screen.findByText("+hi")).toBeInTheDocument();
    const calls = vi.mocked(fetch).mock.calls.map((c) => String(c[0]));
    expect(calls.some((u) => u.includes("/v1/sessions/s1/diff?turn=2"))).toBe(true);
  });

  it("disables rewind and stops fetching while a turn is in flight", async () => {
    routeFetch({ "/diff": diff });
    const turns = [block(1), block(2, false)];
    const { rerender } = render(<ChangesPanel sessionId="s1" turns={turns} turnInFlight={false} />);
    await screen.findByText("+hi");
    const fetches = vi.mocked(fetch).mock.calls.length;
    rerender(<ChangesPanel sessionId="s1" turns={turns} turnInFlight />);
    expect(screen.getByText("rewind turn 1").closest("button")).toBeDisabled();
    expect(screen.getByText("+hi")).toBeInTheDocument();
    expect(vi.mocked(fetch).mock.calls.length).toBe(fetches);
  });

  it("shows an error, never 'no changes', when the diff read fails", async () => {
    routeFetch({ "/diff": null });
    render(<ChangesPanel sessionId="s1" turns={[block(1)]} turnInFlight={false} />);
    expect(await screen.findByText(/Could not load changes/)).toBeInTheDocument();
    expect(screen.queryByText(/No file changes/)).toBeNull();
  });

  it("seeds the review switch from the server and follows a resolved review", async () => {
    routeFetch({ "/diff": diff });
    const { rerender } = render(
      <ChangesPanel sessionId="s1" turns={[block(1)]} turnInFlight={false} reviewWrites />);
    const box = screen.getByLabelText("Review writes before apply") as HTMLInputElement;
    expect(box.checked).toBe(true);
    rerender(<ChangesPanel sessionId="s1" turns={[block(1)]} turnInFlight={false}
      reviewWrites reviewSignal={false} />);
    expect(box.checked).toBe(false);
    await screen.findByText("+hi");
  });

  it("posts revert-file after the reject confirm and rewind after its confirm", async () => {
    const posts = routeFetch({ "/diff": diff, "/rewind": { user_turn: 1 }, "/revert-file": { reverted: true } });
    render(<ChangesPanel sessionId="s1" turns={[block(1), block(2)]} turnInFlight={false} />);
    await screen.findByText("+hi");
    fireEvent.click(screen.getByText("reject"));
    fireEvent.click(screen.getByText("confirm reject"));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toContain("/v1/sessions/s1/revert-file");
    expect(posts[0].body).toEqual({ path: "a.txt", turn: 2 });
    fireEvent.click(await screen.findByText("rewind turn 2"));
    fireEvent.click(screen.getByText("confirm rewind"));
    await vi.waitFor(() => expect(posts).toHaveLength(2));
    expect(posts[1].url).toContain("/rewind");
    expect(posts[1].body).toEqual({ turns: 1 });
  });

  it("toggles review writes", async () => {
    const posts = routeFetch({ "/diff": diff, "/review": { review_writes: true } });
    render(<ChangesPanel sessionId="s1" turns={[block(1)]} turnInFlight={false} />);
    await screen.findByText("+hi");
    fireEvent.click(screen.getByLabelText("Review writes before apply"));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].body).toEqual({ enabled: true });
  });
});
