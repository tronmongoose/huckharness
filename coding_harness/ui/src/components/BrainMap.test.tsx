import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { edgesFor, initNodes, MAX_TICKS, nodeAt, step } from "@/lib/brainLayout";
import type { BrainCluster } from "@/lib/types";
import { routeFetch } from "@/test/route-fetch";
import { BrainMap } from "./BrainMap";

const CLUSTERS: BrainCluster[] = [
  { id: 0, label: "garden compost soil", size: 9, vaults: { hot: 6, cold: 3 }, max_tier: 1,
    near: [[1, 0.8], [2, 0.6], [3, 0.2]] },
  { id: 1, label: "boat keel mooring", size: 4, vaults: { cold: 4 }, max_tier: 2,
    near: [[0, 0.8], [2, 0.55], [3, 0.1]] },
  { id: 2, label: "piano chord sonata", size: 1, vaults: { hot: 1 }, max_tier: 0,
    near: [[0, 0.6], [1, 0.55], [3, 0.1]] },
];
const MAP = {
  available: true, max_tier: 3, pages: 14, clusters: CLUSTERS,
  central: {
    "0": [{ path: "hot/compost.md", title: "Compost log" }, { path: "cold/soil.md", title: "" }],
    "1": [{ path: "cold/keel.md", title: "Keel repair" }],
    "2": [{ path: "hot/sonata.md", title: "Sonata" }],
  },
};

let ctx: Record<string, ReturnType<typeof vi.fn> | unknown>;

beforeEach(() => {
  ctx = {
    setTransform: vi.fn(), clearRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(),
    stroke: vi.fn(), arc: vi.fn(), fill: vi.fn(), fillText: vi.fn(),
  };
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockImplementation(() => ctx as never);
  vi.stubGlobal("matchMedia", (q: string) => ({ matches: q.includes("reduce"),
    addEventListener() {}, removeEventListener() {} }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

function topic(name: RegExp) {
  return within(screen.getByRole("list", { name: "topics" })).getByRole("button", { name });
}

describe("BrainMap", () => {
  it("draws one node per cluster with short labels and lists them", async () => {
    routeFetch({ "/v1/brain/map": MAP });
    render(<BrainMap onOpen={vi.fn()} />);
    expect(await screen.findByText("3 topics · 14 notes")).toBeInTheDocument();
    expect(within(screen.getByRole("list", { name: "topics" })).getAllByRole("button")).toHaveLength(3);
    expect(topic(/garden compost soil, 9 notes/)).toBeInTheDocument();
    const labels = (ctx.fillText as ReturnType<typeof vi.fn>).mock.calls.map((c) => c[0]);
    expect(labels).toEqual(expect.arrayContaining(["garden compost", "boat keel", "piano chord"]));
    expect((ctx.arc as ReturnType<typeof vi.fn>).mock.calls.length).toBeGreaterThanOrEqual(3);
  });

  it("asks for the chosen tier and says when the identity capped it", async () => {
    routeFetch({ "/v1/brain/map": { ...MAP, max_tier: 1 } });
    render(<BrainMap onOpen={vi.fn()} />);
    fireEvent.change(screen.getByLabelText("highest tier shown"), { target: { value: "2" } });
    expect(await screen.findByText("capped at internal by the brain identity")).toBeInTheDocument();
    const urls = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.map((c) => String(c[0]));
    expect(urls.some((u) => u.includes("/v1/brain/map?max_tier=2"))).toBe(true);
  });

  it("highlights the clusters a topic search hits", async () => {
    routeFetch({
      "/v1/brain/map/search": { available: true, hits: { "1": 3, "2": 1 }, warnings: [] },
      "/v1/brain/map": MAP,
    });
    render(<BrainMap onOpen={vi.fn()} />);
    fireEvent.change(await screen.findByLabelText("search topics"), { target: { value: "keel" } });
    fireEvent.submit(screen.getByLabelText("search topics").closest("form")!);
    expect(await screen.findByLabelText("topic hits")).toHaveTextContent("2 topics match");
    expect(topic(/boat keel mooring/)).toHaveAttribute("data-hit", "3");
    expect(topic(/garden/)).toHaveAttribute("data-hit", "0");
    expect(topic(/boat keel mooring, 4 notes, 3 hits/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("clear"));
    expect(screen.queryByLabelText("topic hits")).not.toBeInTheDocument();
  });

  it("opens a side panel on click with the vault mix and central notes", async () => {
    routeFetch({ "/v1/brain/map": MAP });
    const onOpen = vi.fn();
    render(<BrainMap onOpen={onOpen} />);
    await screen.findByText("3 topics · 14 notes");
    fireEvent.click(topic(/garden compost soil/));
    const panel = screen.getByRole("complementary", { name: "topic detail" });
    expect(within(panel).getByText("garden compost soil")).toBeInTheDocument();
    expect(within(panel).getByText(/9 notes/)).toBeInTheDocument();
    const mix = within(panel).getByRole("list", { name: "vault mix" });
    expect(within(mix).getAllByRole("listitem").map((li) => li.textContent)).toEqual(["hot6", "cold3"]);
    expect(within(panel).getByText("Compost log")).toBeInTheDocument();
    expect(within(panel).getByText("soil")).toBeInTheDocument();
    fireEvent.click(within(panel).getAllByText("open")[0]);
    expect(onOpen).toHaveBeenCalledWith("hot/compost.md");
    fireEvent.click(within(panel).getByText("close"));
    expect(screen.queryByRole("complementary", { name: "topic detail" })).not.toBeInTheDocument();
  });

  it("shows a tooltip on hover and selects on a canvas click", async () => {
    routeFetch({ "/v1/brain/map": { ...MAP, clusters: [CLUSTERS[1]] } });
    render(<BrainMap onOpen={vi.fn()} />);
    const canvas = await screen.findByLabelText("topic map");
    // jsdom has no layout, so the map uses its 800x520 fallback and the same pure layout.
    const [node] = initNodes([CLUSTERS[1]], 800, 520);
    for (let t = 0; t < MAX_TICKS; t++) step([node], [], 800, 520, t);
    fireEvent.mouseMove(canvas, { clientX: node.x, clientY: node.y });
    expect(screen.getByRole("tooltip")).toHaveTextContent("boat keel mooring · 4 notes");
    fireEvent.mouseMove(canvas, { clientX: 1, clientY: 1 });
    expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
    fireEvent.click(canvas, { clientX: node.x, clientY: node.y });
    expect(screen.getByRole("complementary", { name: "topic detail" })).toBeInTheDocument();
  });

  it("says when the backend has no map", async () => {
    routeFetch({ "/v1/brain/map": { available: false, reason: "the topic map needs the in-process backend" } });
    render(<BrainMap onOpen={vi.fn()} />);
    expect(await screen.findByText(/needs the in-process backend/)).toBeInTheDocument();
  });
});

describe("brainLayout", () => {
  it("keeps the top two edges above the threshold, once each", () => {
    expect(edgesFor(CLUSTERS)).toEqual([[0, 1, 0.8], [0, 2, 0.6], [1, 2, 0.55]]);
    expect(edgesFor(CLUSTERS, 0.7)).toEqual([[0, 1, 0.8]]);
  });

  it("is deterministic, stays in bounds and hit-tests nodes", () => {
    const run = () => {
      const nodes = initNodes(CLUSTERS, 600, 400);
      for (let t = 0; t < MAX_TICKS; t++) step(nodes, edgesFor(CLUSTERS), 600, 400, t);
      return nodes;
    };
    const a = run();
    expect(run()).toEqual(a);
    for (const n of a) {
      expect(n.x).toBeGreaterThan(0);
      expect(n.x).toBeLessThan(600);
      expect(n.y).toBeGreaterThan(0);
      expect(n.y).toBeLessThan(400);
    }
    expect(nodeAt(a, a[1].x, a[1].y)?.id).toBe(a[1].id);
    expect(nodeAt(a, -100, -100)).toBeNull();
  });
});
