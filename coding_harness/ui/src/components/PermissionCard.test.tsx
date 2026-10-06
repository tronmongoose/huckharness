import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PendingPermission } from "@/lib/types";
import { PermissionCard } from "./PermissionCard";

const request: PendingPermission = {
  req_id: "abc123",
  tool: "Write",
  args_preview: { file_path: "/etc/hosts" },
  reason: "out_of_envelope:Write:write:/etc/hosts",
  created_at: new Date().toISOString(),
};

function mockFetch() {
  const calls: Array<{ url: string; body: unknown }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      calls.push({
        url: String(url),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      return new Response(JSON.stringify({ ok: true }), { status: 200 });
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("PermissionCard", () => {
  it("renders tool, args, reason, and all three actions", () => {
    mockFetch();
    render(<PermissionCard sessionId="s1" request={request} />);
    expect(screen.getByText("Write")).toBeInTheDocument();
    expect(screen.getByText("/etc/hosts")).toBeInTheDocument();
    expect(screen.getByText(/out_of_envelope/)).toBeInTheDocument();
    expect(screen.getByText("allow once")).toBeInTheDocument();
    expect(screen.getByText("allow always")).toBeInTheDocument();
    expect(screen.getByText("deny")).toBeInTheDocument();
  });

  it("posts allow_once to the resolve endpoint", async () => {
    const calls = mockFetch();
    render(<PermissionCard sessionId="s1" request={request} />);
    fireEvent.click(screen.getByText("allow once"));
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url).toContain("/v1/sessions/s1/permissions/abc123");
    expect(calls[0].body).toEqual({ decision: "allow_once" });
  });

  it("posts allow_always with expiry_minutes", async () => {
    const calls = mockFetch();
    render(<PermissionCard sessionId="s1" request={request} />);
    fireEvent.change(screen.getByLabelText("grant expiry minutes"), {
      target: { value: "15" },
    });
    fireEvent.click(screen.getByText("allow always"));
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({
      decision: "allow_always",
      expiry_minutes: 15,
    });
  });

  it("posts deny", async () => {
    const calls = mockFetch();
    render(<PermissionCard sessionId="s1" request={request} />);
    fireEvent.click(screen.getByText("deny"));
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ decision: "deny" });
  });
});

describe("PermissionCard review variant", () => {
  const review: PendingPermission = {
    req_id: "rv1",
    tool: "Edit",
    args_preview: {
      file_path: "/w/a.py",
      summary: "Edit a.py (1 replacement)",
      diff: "--- a/w/a.py\n+++ b/w/a.py\n@@ -1 +1 @@\n-old\n+new",
    },
    reason: "review",
    created_at: new Date().toISOString(),
    kind: "review",
  };

  it("renders the diff inline with apply, skip and apply all", () => {
    mockFetch();
    render(<PermissionCard sessionId="s1" request={review} />);
    expect(screen.getByText("review write")).toBeInTheDocument();
    expect(screen.getByText("+new").className).toContain("diff-add");
    expect(screen.getByText("-old").className).toContain("diff-del");
    expect(screen.queryByText("allow once")).toBeNull();
    expect(screen.queryByLabelText("grant expiry minutes")).toBeNull();
  });

  it.each([
    ["apply", { decision: "allow_once" }],
    ["skip", { decision: "deny" }],
    ["apply all", { decision: "allow_always" }],
  ])("%s posts its decision", async (label, body) => {
    const calls = mockFetch();
    render(<PermissionCard sessionId="s1" request={review} />);
    fireEvent.click(screen.getByText(label));
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url).toContain("/v1/sessions/s1/permissions/rv1");
    expect(calls[0].body).toEqual(body);
  });
});
