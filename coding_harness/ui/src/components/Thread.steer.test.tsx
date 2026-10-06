import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import type { TurnBlock } from "@/lib/types";
import { Thread } from "./Thread";

afterEach(() => vi.unstubAllGlobals());

const open: TurnBlock = {
  turn: 1, prompt: "go", streamingText: "", steps: [], done: false,
  items: [{ kind: "steer", text: "keep it short", applied: false }],
};

function renderThread(turnInFlight: boolean) {
  render(
    <Thread sessionId="s1" thread={[open]} pending={[]} turnInFlight={turnInFlight}
      phase={{ kind: "idle", since: 0, detail: "", tokens: 0 }}
      disabled={false} onNewSession={vi.fn()} />,
  );
  return screen.getByRole("textbox");
}

describe("Thread steering", () => {
  it("posts to /steer, not /turn, while a turn runs", async () => {
    const posts = routeFetch({ "/steer": { status: "queued" } });
    const box = renderThread(true);
    fireEvent.change(box, { target: { value: "also update the docs" } });
    fireEvent.click(screen.getByRole("button", { name: "steer" }));
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toContain("/v1/sessions/s1/steer");
    expect(posts[0].body).toEqual({ message: "also update the docs" });
  });

  it("posts to /turn when idle and shows no steer button", async () => {
    const posts = routeFetch({ "/turn": { session_id: "s1" } });
    const box = renderThread(false);
    fireEvent.change(box, { target: { value: "next task" } });
    expect(screen.queryByRole("button", { name: "steer" })).toBeNull();
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toContain("/v1/sessions/s1/turn");
  });

  it("does not post an empty steer mid-turn", async () => {
    const posts = routeFetch({});
    const box = renderThread(true);
    fireEvent.keyDown(box, { key: "Enter" });
    await new Promise((r) => setTimeout(r, 20));
    expect(posts).toEqual([]);
  });

  it("renders a queued steer note in the turn", () => {
    renderThread(true);
    expect(screen.getByText("steer queued")).toBeInTheDocument();
    expect(screen.getByText("keep it short")).toBeInTheDocument();
  });
});
