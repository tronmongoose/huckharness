import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import { AttachChips } from "./AttachChips";
import { BrainView } from "./BrainView";

afterEach(() => vi.unstubAllGlobals());

describe("BrainView", () => {
  it("shows the settings block when no brain is configured", async () => {
    routeFetch({ "/v1/brain/status": { configured: false, backend: null, ok: false, age_hours: null, stale: false, warnings: [] } });
    render(<BrainView onAttach={vi.fn()} />);
    expect(await screen.findByText("No second brain configured")).toBeInTheDocument();
    expect(screen.getByText(/"-m", "slos_recall\.server"/)).toBeInTheDocument();
  });

  it("searches, badges local-only tiers, opens a note, and attaches it", async () => {
    routeFetch({
      "/v1/brain/status": { configured: true, backend: "mcp", ok: true, age_hours: null, stale: false, warnings: [] },
      "/v1/brain/search": { hits: [
        { path: "finance/budget.md", vault: "finance", sensitivity: "confidential", tier: 2, score: 1, snippet: "budget" },
        { path: "startup/plan.md", vault: "startup", sensitivity: "internal", tier: 1, score: 1, snippet: "plan" },
      ] },
      "/v1/brain/page": { path: "finance/budget.md", sensitivity: "confidential", tier: 2, content: "# Budget" },
    });
    const onAttach = vi.fn();
    render(<BrainView onAttach={onAttach} />);
    fireEvent.click(await screen.findByText("list"));
    fireEvent.change(await screen.findByLabelText("search the brain"), { target: { value: "budget" } });
    fireEvent.submit(screen.getByLabelText("search the brain").closest("form")!);
    const badge = await screen.findByText("confidential");
    expect(badge).toHaveClass("text-accent");
    expect(screen.getByText("internal")).toHaveClass("text-muted");
    fireEvent.click(screen.getByText("finance/budget.md"));
    fireEvent.click(await screen.findByText("attach to session"));
    expect(onAttach).toHaveBeenCalledWith({ path: "finance/budget.md", tier: 2 });
  });

  it("shows the backend and the index age, and marks a stale index", async () => {
    routeFetch({
      "/v1/brain/status": { configured: true, backend: "inprocess", ok: true, age_hours: 40.2,
        stale: true, warnings: [] },
    });
    render(<BrainView onAttach={vi.fn()} />);
    const line = await screen.findByLabelText("index status");
    expect(line).toHaveTextContent("inprocess");
    expect(line).toHaveTextContent("indexed 40h ago");
    expect(screen.getByText("stale")).toHaveClass("text-danger");
  });

  it("lists the index warnings when there are any", async () => {
    routeFetch({
      "/v1/brain/status": { configured: true, backend: "inprocess", ok: true, age_hours: 2,
        stale: false, warnings: ["embedding timed out after 5s; keyword search only for the next 60s"] },
    });
    render(<BrainView onAttach={vi.fn()} />);
    const list = await screen.findByLabelText("index warnings");
    expect(list).toHaveTextContent("keyword search only");
  });

  it("clamps a long snippet to two lines with no display utility overriding it", async () => {
    routeFetch({
      "/v1/brain/status": { configured: true, backend: "mcp", ok: true, age_hours: null, stale: false, warnings: [] },
      "/v1/brain/search": { hits: [
        { path: "a.md", vault: "v", sensitivity: "internal", tier: 1, score: 1, snippet: "long ".repeat(200) },
      ] },
    });
    render(<BrainView onAttach={vi.fn()} />);
    fireEvent.click(await screen.findByText("list"));
    fireEvent.change(await screen.findByLabelText("search the brain"), { target: { value: "x" } });
    fireEvent.submit(screen.getByLabelText("search the brain").closest("form")!);
    const snippet = await screen.findByText(/^(long )+/);
    expect(snippet).toHaveClass("line-clamp-2");
    // Tailwind orders display after line-clamp, so any of these would win.
    for (const display of ["block", "inline-block", "flex", "grid", "inline"]) {
      expect(snippet).not.toHaveClass(display);
    }
  });
});

describe("AttachChips", () => {
  it("says a confidential attachment keeps the session local", () => {
    const onRemove = vi.fn();
    render(<AttachChips items={[{ path: "finance/budget.md", tier: 2 }]} onRemove={onRemove} />);
    expect(screen.getByText("sending this keeps the session on local models")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("remove finance/budget.md"));
    expect(onRemove).toHaveBeenCalledWith("finance/budget.md");
  });

  it("stays quiet for internal notes", () => {
    render(<AttachChips items={[{ path: "startup/plan.md", tier: 1 }]} onRemove={vi.fn()} />);
    expect(screen.queryByText(/keeps the session/)).toBeNull();
  });
});

describe("BrainView map", () => {
  it("opens on the map and reads a central note in the reader", async () => {
    vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"),
      addEventListener() {}, removeEventListener() {} }));
    routeFetch({
      "/v1/brain/status": { configured: true, backend: "inprocess", ok: true, age_hours: 1, stale: false, warnings: [] },
      "/v1/brain/map": { available: true, max_tier: 3, pages: 2, central: { "0": [{ path: "startup/plan.md", title: "Plan" }] },
        clusters: [{ id: 0, label: "launch plan", size: 2, vaults: { startup: 2 }, max_tier: 1, near: [] }] },
      "/v1/brain/page": { path: "startup/plan.md", sensitivity: "internal", tier: 1, content: "# Plan body" },
    });
    render(<BrainView onAttach={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /launch plan, 2 notes/ }));
    fireEvent.click(screen.getByText("open"));
    expect(await screen.findByText("Plan body")).toBeInTheDocument();
    expect(screen.queryByLabelText("search the brain")).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("list"));
    expect(screen.getByLabelText("search the brain")).toBeInTheDocument();
  });
});
