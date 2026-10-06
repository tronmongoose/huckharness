import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { SpecStatus } from "@/lib/types";
import { SpecPanel } from "./SpecPanel";

afterEach(() => vi.unstubAllGlobals());

const SPEC: SpecStatus = { version: 0, bytes: null, error: null };

// Answers GET .../plan with `plan` and records every non-GET call.
function stubServer(plan: { exists: boolean; text: string }) {
  const calls: Array<{ method: string; url: string; body: unknown }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    if (method === "GET") return new Response(JSON.stringify(plan), { status: 200 });
    calls.push({ method, url: String(url), body: JSON.parse(String(init?.body ?? "{}")) });
    return new Response(JSON.stringify({ status: "accepted" }), { status: 202 });
  }));
  return calls;
}

describe("SpecPanel", () => {
  it("drafts a plan from a goal", async () => {
    const calls = stubServer({ exists: false, text: "" });
    render(<SpecPanel sessionId="s1" turnInFlight={false} spec={SPEC} todos={[]} />);
    expect(await screen.findByText("no plan")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("goal"), { target: { value: "add a widget" } });
    fireEvent.click(screen.getByText("draft plan"));
    await vi.waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({ method: "POST", url: expect.stringContaining("/v1/sessions/s1/plan"),
      body: { goal: "add a widget" } });
  });

  it("saves an edit, then runs at the chosen level", async () => {
    const calls = stubServer({ exists: true, text: "1. old step" });
    render(<SpecPanel sessionId="s1" turnInFlight={false} spec={SPEC} todos={[]} />);
    expect(await screen.findByDisplayValue("1. old step")).toBeInTheDocument();
    expect(screen.getByText("saved")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("plan"), { target: { value: "1. new step" } });
    expect(screen.getByText("unsaved")).toBeInTheDocument();
    fireEvent.click(screen.getByText("medium"));
    fireEvent.click(screen.getByText("run plan"));
    await vi.waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[0]).toMatchObject({ method: "PUT", body: { text: "1. new step" } });
    expect(calls[1]).toMatchObject({ method: "POST", url: expect.stringContaining("/act"),
      body: { autonomy: "medium" } });
  });

  it("disables every action while a turn runs", async () => {
    stubServer({ exists: true, text: "a plan" });
    render(<SpecPanel sessionId="s1" turnInFlight spec={SPEC} todos={[]} />);
    await screen.findByDisplayValue("a plan");
    expect(screen.getByText("run plan")).toBeDisabled();
    expect(screen.getByText("draft plan")).toBeDisabled();
    expect(screen.getByLabelText("plan")).toBeDisabled();
  });

  it("shows why planning failed", async () => {
    stubServer({ exists: false, text: "" });
    render(<SpecPanel sessionId="s1" turnInFlight={false} todos={[]}
      spec={{ ...SPEC, error: "the turn produced no plan text" }} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("no plan text");
  });
});
