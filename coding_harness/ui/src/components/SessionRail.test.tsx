import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SessionRail } from "./SessionRail";

function mockFetch() {
  const calls: Array<{ url: string; body: Record<string, unknown> }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      // The rail also polls its lists; only session creation is under test.
      if (init?.method !== "POST") return new Response("{}", { status: 200 });
      calls.push({
        url: String(url),
        body: init?.body ? JSON.parse(String(init.body)) : {},
      });
      return new Response(JSON.stringify({ session_id: "s1" }), { status: 200 });
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

function renderRail() {
  const onCreated = vi.fn();
  render(
    <SessionRail sessions={[]} activeId={null} onSelect={vi.fn()} onCreated={onCreated} />,
  );
  return onCreated;
}

describe("SessionRail", () => {
  it("creates interactive sessions so approvals can reach the operator", async () => {
    // The permission pane is unreachable without this: serve defaults
    // interactive to false, builds no broker, and answers /permissions 409.
    const calls = mockFetch();
    const onCreated = renderRail();

    fireEvent.click(screen.getByRole("button", { name: /new session/i }));
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    expect(calls[0].url).toContain("/v1/sessions");
    expect(calls[0].body.interactive).toBe(true);
    // The default is the server's preset for its own cwd: no spec is sent.
    expect(calls[0].body.envelope).toBeUndefined();
    await vi.waitFor(() => expect(onCreated).toHaveBeenCalledWith("s1"));
  });

  it("sends the chosen autonomy level and no conflicting mode", async () => {
    const calls = mockFetch();
    renderRail();

    fireEvent.click(screen.getByRole("button", { name: "options" }));
    fireEvent.click(screen.getByRole("button", { name: "medium" }));
    fireEvent.click(screen.getByRole("button", { name: /create session/i }));
    await vi.waitFor(() => expect(calls).toHaveLength(1));

    expect(calls[0].body.autonomy).toBe("medium");
    // The server derives mode from the level; sending both lets one silently
    // override the other.
    expect(calls[0].body.mode).toBeUndefined();
    await vi.waitFor(() =>
      expect(screen.getByRole("button", { name: /create session/i })).toBeEnabled(),
    );
  });

  it("defaults to low, and names the mode each level implies", () => {
    renderRail();
    fireEvent.click(screen.getByRole("button", { name: "options" }));
    expect(screen.getByRole("button", { name: "low" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByText(/act mode/i)).toBeInTheDocument();

    // One assertion on the whole hint line: "read-only" alone also matches the
    // read-only-repo envelope description.
    fireEvent.click(screen.getByRole("button", { name: "off" }));
    expect(screen.getByText("read-only · plan mode")).toBeInTheDocument();
  });
});
