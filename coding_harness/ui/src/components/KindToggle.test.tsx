import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import type { SessionSummary } from "@/lib/types";
import { Header } from "./Header";
import { SessionRail } from "./SessionRail";
import { stepSummary } from "./ToolStep";

afterEach(() => vi.unstubAllGlobals());

const session: SessionSummary = {
  session_id: "s1", mode: "act", model: "mistral-small3.2:latest", identity: null,
  revoked: false, has_envelope: true, closed: false, kind: "code",
};

describe("session kind", () => {
  it("the header toggle switches a session to chat", async () => {
    const posts = routeFetch({ "/v1/models": { models: [] }, "/v1/projects": { projects: [] } });
    render(<Header session={session} cwd="/p" conn="live" turnInFlight={false} paneOpen={false}
      onTogglePane={vi.fn()} pendingCount={0} context={null} />);
    expect(screen.getByRole("button", { name: "code" })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "chat" }));
    await vi.waitFor(() => expect(posts.some((p) => p.url.includes("/v1/sessions/s1/kind"))).toBe(true));
    expect(posts.find((p) => p.url.includes("/kind"))?.body).toEqual({ kind: "chat" });
  });

  it("new chat creates a chat session", async () => {
    const posts = routeFetch({ "/v1/sessions": { session_id: "c1" } });
    const onCreated = vi.fn();
    render(<SessionRail sessions={[]} activeId={null} onSelect={vi.fn()} onCreated={onCreated} />);
    fireEvent.click(screen.getByText("new chat"));
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith("c1"));
    expect(posts[0].body).toMatchObject({ kind: "chat", interactive: true });
  });

  it("an Explore step is summarized by its task", () => {
    expect(stepSummary({ tool: "Explore", argsPreview: "", args: { task: "where is x?" } }))
      .toBe("where is x?");
  });
});
