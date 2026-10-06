import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, onTestFinished, vi } from "vitest";

import type { TurnBlock } from "@/lib/types";
import { Thread } from "./Thread";

function mockFetch() {
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(String(url));
      return new Response("{}", { status: 200 });
    }),
  );
  return urls;
}

afterEach(() => vi.unstubAllGlobals());

const turn: TurnBlock = {
  turn: 1, prompt: "go", streamingText: "", steps: [], done: false,
  items: [{ kind: "text", text: "Run `make test`:\n\n```sh\nmake test\n```" }],
};

function renderThread(over: Partial<Parameters<typeof Thread>[0]> = {}) {
  const onNewSession = vi.fn();
  render(
    <Thread sessionId="s1" thread={[]} pending={[]} turnInFlight={false}
      phase={{ kind: "idle", since: 0, detail: "", tokens: 0 }}
      disabled={false} onNewSession={onNewSession} {...over} />,
  );
  return { onNewSession, box: screen.getByRole("textbox") };
}

describe("Thread", () => {
  it("shows a sent prompt at once, marks it received, then hands over to the real turn", async () => {
    let accept: (r: Response) => void = () => {};
    vi.stubGlobal("matchMedia", () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>((r) => { accept = r; })));
    const props = { sessionId: "s1", pending: [], turnInFlight: false, disabled: false,
      onNewSession: vi.fn(), phase: { kind: "idle" as const, since: 0, detail: "", tokens: 0 } };
    const view = render(<Thread {...props} thread={[]} />);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "hello there" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(await screen.findByTestId("pending-turn")).toHaveTextContent("hello there");
    expect(screen.getByText("sending")).toBeInTheDocument();
    accept(new Response("{}", { status: 202 }));
    expect(await screen.findByText("received ✓")).toBeInTheDocument();
    const real = { turn: 1, prompt: "hello there", streamingText: "", steps: [], items: [], done: false };
    view.rerender(<Thread {...props} thread={[real]} turnInFlight
      phase={{ kind: "starting", since: Date.now(), detail: "", tokens: 0 }} />);
    expect(screen.queryByTestId("pending-turn")).toBeNull();
    expect(screen.getAllByText("hello there")).toHaveLength(1);
  });

  it("scrolls a pending review card into view on mount", () => {
    const seen: Element[] = [];
    const spy = vi.fn(function (this: Element) { seen.push(this); });
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    onTestFinished(() => { Element.prototype.scrollIntoView = original; });
    renderThread({
      thread: [turn],
      pending: [{
        req_id: "r1", tool: "Write", reason: "review", created_at: "", kind: "review",
        args_preview: { file_path: "a.py", summary: "Write a.py", diff: "+x" },
      }],
    });
    expect(spy).toHaveBeenCalledWith({ block: "start" });
    expect((seen[0] as HTMLElement).dataset.reqId).toBe("r1");
  });

  it("renders agent prose as markdown", () => {
    renderThread({ thread: [turn] });
    // Highlighting splits the block into spans, so match the code element's whole text.
    expect(screen.getByText((_, el) => el?.matches("pre code") === true && el.textContent === "make test"))
      .toBeInTheDocument();
  });

  it("routes /model to the session, not to the agent", async () => {
    const urls = mockFetch();
    const { box } = renderThread();
    fireEvent.change(box, { target: { value: "/model gpt-oss:20b" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(urls).toHaveLength(1));
    expect(urls[0]).toContain("/v1/sessions/s1/model");
    await vi.waitFor(() => expect(screen.getByText("model → gpt-oss:20b")).toBeInTheDocument());
  });

  it("/skill sends the skill and the task separately", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", vi.fn(async (_u: string, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body ?? "{}")));
      return new Response("{}", { status: 200 });
    }));
    const { box } = renderThread();
    fireEvent.change(box, { target: { value: "/skill design-tenets restyle the header" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ skill: "design-tenets", message: "restyle the header" });
  });

  it("shows a skill turn as a chip, the task, and who picked the model", () => {
    renderThread({ thread: [{
      ...turn, done: true, haltedReason: "model_done", tokensOut: 5,
      prompt: "[skill: demo-film]\nbody\n\n---\n\nTask: film it",
      model: "gpt-oss:20b", routeReason: "override_explicit_model",
    }] });
    expect(screen.getByText("skill demo-film")).toBeInTheDocument();
    expect(screen.getByText("film it")).toBeInTheDocument();
    expect(screen.getByText("gpt-oss:20b · pinned · model_done · 5 tokens out")).toBeInTheDocument();
  });

  it("sends queued notes with the prompt and clears them after", async () => {
    const bodies: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", vi.fn(async (_u: string, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body ?? "{}")));
      return new Response("{}", { status: 200 });
    }));
    const onAttachmentsChange = vi.fn();
    const { box } = renderThread({ attachments: [{ path: "finance/budget.md", tier: 2 }],
      onAttachmentsChange });
    fireEvent.change(box, { target: { value: "summarize it" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toEqual({ message: "summarize it", attach: ["finance/budget.md"] });
    await vi.waitFor(() => expect(onAttachmentsChange).toHaveBeenCalledWith([]));
  });

  it("/new asks for a fresh session", async () => {
    const { box, onNewSession } = renderThread();
    fireEvent.change(box, { target: { value: "/new" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await vi.waitFor(() => expect(onNewSession).toHaveBeenCalled());
  });

  it("Esc interrupts a running turn and the button becomes stop", async () => {
    const urls = mockFetch();
    const { box } = renderThread({ turnInFlight: true });
    expect(screen.getByRole("button", { name: "stop" })).toBeInTheDocument();
    fireEvent.keyDown(box, { key: "Escape" });
    await vi.waitFor(() => expect(urls[0]).toContain("/v1/sessions/s1/interrupt"));
  });

  it("a finished turn's files-changed footer opens the changes tab for it", () => {
    mockFetch();
    const onOpenChanges = vi.fn();
    const done: TurnBlock = { ...turn, turn: 3, done: true, filesChanged: ["/w/a", "/w/b"] };
    renderThread({ thread: [done, { ...turn, turn: 4, done: true, filesChanged: [] }], onOpenChanges });
    expect(screen.getAllByText(/files? changed/)).toHaveLength(1);
    fireEvent.click(screen.getByText("2 files changed"));
    expect(onOpenChanges).toHaveBeenCalledWith(3);
  });

  it("shows a dismissible banner on a resumed thread", () => {
    renderThread({ sessionId: "20260102T030405-ab", thread: [turn],
      resumed: { sessionId: "20260102T030405-ab", turns: 4 } });
    const banner = screen.getByRole("status");
    expect(banner.textContent).toMatch(/^Resumed from .*2026.*, 4 turns/);
    fireEvent.click(screen.getByLabelText("dismiss"));
    expect(screen.queryByRole("status")).toBeNull();
  });

  it("shows no banner on a fresh thread", () => {
    renderThread({ thread: [turn] });
    expect(screen.queryByText(/Resumed from/)).toBeNull();
  });
});
