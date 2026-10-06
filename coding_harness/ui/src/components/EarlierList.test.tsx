import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TranscriptInfo } from "@/lib/types";
import { EarlierList } from "./EarlierList";

afterEach(() => vi.unstubAllGlobals());

function row(id: string, title: string, extra: Partial<TranscriptInfo> = {}): TranscriptInfo {
  const now = new Date().toISOString();
  return {
    id, session_id: id, title, modified: now, started: now, first_prompt: title,
    turns: 3, bytes: 100, resumable: true, reason_if_not: null, ...extra,
  };
}

// Answers GET /v1/transcripts from `list(url)` and records every POST.
function stub(list: (url: string) => TranscriptInfo[], postStatus = 200, postBody: unknown = {}) {
  const posts: Array<{ url: string; body: unknown }> = [];
  const gets: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") {
      posts.push({ url: String(url), body: JSON.parse(String(init.body)) });
      return new Response(JSON.stringify(postBody), { status: postStatus });
    }
    gets.push(String(url));
    return new Response(JSON.stringify({ transcripts: list(String(url)) }), { status: 200 });
  }));
  return { posts, gets };
}

describe("EarlierList", () => {
  it("resumes a past session interactively and opens it", async () => {
    const { posts } = stub(() => [row("s-old", "fix the parser")]);
    const onOpened = vi.fn();
    render(<EarlierList onOpened={onOpened} />);
    fireEvent.click(await screen.findByText("fix the parser"));
    await vi.waitFor(() => expect(onOpened).toHaveBeenCalledWith("s-old"));
    expect(posts[0]).toEqual({ url: expect.stringContaining("/v1/sessions/s-old/resume"),
      body: { interactive: true } });
    expect(screen.getByText(/3 turns/)).toBeTruthy();
  });

  it("searches with a debounced query", async () => {
    const { gets } = stub((url) => url.includes("q=lexer")
      ? [row("s-2", "the lexer")] : [row("s-1", "the parser"), row("s-2", "the lexer")]);
    render(<EarlierList onOpened={vi.fn()} />);
    await screen.findByText("the parser");
    fireEvent.change(screen.getByLabelText("search history"), { target: { value: "lexer" } });
    await vi.waitFor(() => expect(screen.queryByText("the parser")).toBeNull());
    expect(gets.some((u) => u.endsWith("/v1/transcripts?q=lexer"))).toBe(true);
  });

  it("renames inline on Enter", async () => {
    const { posts } = stub(() => [row("s-1", "the parser")]);
    render(<EarlierList onOpened={vi.fn()} />);
    fireEvent.click(await screen.findByLabelText("rename the parser"));
    const input = screen.getByLabelText("session title");
    fireEvent.change(input, { target: { value: "parser rewrite" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ url: expect.stringContaining("/v1/transcripts/s-1/title"),
      body: { title: "parser rewrite" } });
  });

  it("deletes only after the inline confirm", async () => {
    const confirmSpy = vi.fn();
    vi.stubGlobal("confirm", confirmSpy);
    const { posts } = stub(() => [row("s-1", "the parser")]);
    render(<EarlierList onOpened={vi.fn()} />);
    fireEvent.click(await screen.findByLabelText("delete the parser"));
    expect(posts).toHaveLength(0);
    fireEvent.click(screen.getByText("yes"));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].url).toContain("/v1/transcripts/s-1/delete");
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("says why a session cannot resume instead of trying", async () => {
    const { posts } = stub(() => [row("s-1", "old work",
      { resumable: false, reason_if_not: "envelope_expired" })]);
    render(<EarlierList onOpened={vi.fn()} />);
    expect(await screen.findByText(/envelope expired/)).toBeTruthy();
    fireEvent.click(screen.getByText("old work"));
    expect(posts).toHaveLength(0);
  });

  it("cancels a rename on Escape and on blur without saving", async () => {
    const { posts } = stub(() => [row("s-1", "the parser")]);
    render(<EarlierList onOpened={vi.fn()} />);
    fireEvent.click(await screen.findByLabelText("rename the parser"));
    fireEvent.change(screen.getByLabelText("session title"), { target: { value: "nope" } });
    fireEvent.keyDown(screen.getByLabelText("session title"), { key: "Escape" });
    expect(screen.queryByLabelText("session title")).toBeNull();
    fireEvent.click(screen.getByLabelText("rename the parser"));
    fireEvent.blur(screen.getByLabelText("session title"));
    expect(screen.queryByLabelText("session title")).toBeNull();
    expect(posts).toHaveLength(0);
    expect(screen.getByText("the parser")).toBeTruthy();
  });

  it("shows the server's reason when a delete is refused", async () => {
    stub(() => [row("s-1", "the parser")], 409,
      { error: { code: 409, message: "session belongs to another project" } });
    render(<EarlierList onOpened={vi.fn()} />);
    fireEvent.click(await screen.findByLabelText("delete the parser"));
    fireEvent.click(screen.getByText("yes"));
    expect(await screen.findByText(/delete failed: session belongs to another project/)).toBeTruthy();
  });
});
