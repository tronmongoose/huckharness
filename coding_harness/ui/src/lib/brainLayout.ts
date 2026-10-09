// Force layout for the Brain topic map. No graph library: a small
// hand-written simulation (center pull, pairwise repulsion with a collision
// floor, springs on edges, damping, soft walls). Start positions come from a
// golden-angle spiral, not Math.random, so one index always draws the same map.

import type { BrainCluster } from "./types";

export interface LayoutNode {
  id: number;
  r: number;
  x: number;
  y: number;
  vx: number;
  vy: number;
}

export type Edge = [number, number, number]; // [a, b, similarity], a < b

export const EDGE_MIN_SIM = 0.5;
export const EDGES_PER_NODE = 2;
export const MAX_TICKS = 300;
const MIN_R = 10;
const MAX_R = 38;
const PAD = 24;

// Radius by sqrt(size), so area tracks the page count.
export function radiusFor(size: number, maxSize: number): number {
  return MIN_R + (MAX_R - MIN_R) * Math.sqrt(size / Math.max(1, maxSize));
}

// Each node's two most similar neighbours above the threshold, deduplicated.
export function edgesFor(clusters: BrainCluster[], minSim = EDGE_MIN_SIM): Edge[] {
  const ids = new Set(clusters.map((c) => c.id));
  const seen = new Map<string, Edge>();
  for (const c of clusters) {
    const near = c.near.filter(([j, sim]) => ids.has(j) && j !== c.id && sim >= minSim);
    for (const [j, sim] of near.slice(0, EDGES_PER_NODE)) {
      const a = Math.min(c.id, j);
      const b = Math.max(c.id, j);
      seen.set(`${a}-${b}`, [a, b, sim]);
    }
  }
  return [...seen.values()];
}

// Largest clusters nearest the middle, on a golden-angle spiral.
export function initNodes(clusters: BrainCluster[], width: number, height: number): LayoutNode[] {
  const maxSize = Math.max(1, ...clusters.map((c) => c.size));
  const order = [...clusters].sort((a, b) => b.size - a.size || a.id - b.id);
  const step = Math.PI * (3 - Math.sqrt(5));
  const spread = Math.min(width, height) / 2.4;
  return order.map((c, i) => {
    const rad = spread * Math.sqrt((i + 0.5) / order.length);
    return {
      id: c.id, r: radiusFor(c.size, maxSize),
      x: width / 2 + rad * Math.cos(i * step), y: height / 2 + rad * Math.sin(i * step),
      vx: 0, vy: 0,
    };
  });
}

function repel(nodes: LayoutNode[], alpha: number): void {
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i];
      const b = nodes[j];
      const dx = b.x - a.x || 0.01;
      const dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 1;
      const gap = a.r + b.r + 14;
      let force = Math.min(6, 1800 / (dist * dist)) * alpha;
      if (dist < gap) force += (gap - dist) * 0.25;
      const fx = (dx / dist) * force;
      const fy = (dy / dist) * force;
      a.vx -= fx;
      a.vy -= fy;
      b.vx += fx;
      b.vy += fy;
    }
  }
}

function pull(nodes: LayoutNode[], edges: Edge[], alpha: number): void {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (const [ia, ib, sim] of edges) {
    const a = byId.get(ia);
    const b = byId.get(ib);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const dist = Math.sqrt(dx * dx + dy * dy) || 1;
    const rest = a.r + b.r + 40;
    const force = (dist - rest) * 0.004 * (0.5 + sim) * alpha;
    a.vx += (dx / dist) * force;
    a.vy += (dy / dist) * force;
    b.vx -= (dx / dist) * force;
    b.vy -= (dy / dist) * force;
  }
}

// One simulation tick; `tick` counts from 0 to MAX_TICKS and cools the forces.
export function step(nodes: LayoutNode[], edges: Edge[], width: number, height: number,
  tick: number): void {
  const alpha = Math.max(0.02, 1 - tick / MAX_TICKS);
  for (const n of nodes) {
    n.vx += (width / 2 - n.x) * 0.0015 * alpha;
    n.vy += (height / 2 - n.y) * 0.0015 * alpha;
  }
  repel(nodes, alpha);
  pull(nodes, edges, alpha);
  for (const n of nodes) {
    n.vx *= 0.82;
    n.vy *= 0.82;
    n.x += n.vx;
    n.y += n.vy;
    n.x = Math.min(width - PAD - n.r / 2, Math.max(PAD + n.r / 2, n.x));
    n.y = Math.min(height - PAD - n.r / 2, Math.max(PAD + n.r / 2, n.y));
  }
}

// The topmost node under a point, or null.
export function nodeAt(nodes: LayoutNode[], x: number, y: number): LayoutNode | null {
  for (let i = nodes.length - 1; i >= 0; i--) {
    const n = nodes[i];
    const dx = n.x - x;
    const dy = n.y - y;
    if (dx * dx + dy * dy <= (n.r + 3) * (n.r + 3)) return n;
  }
  return null;
}

// The vault holding most of a cluster's pages; ties go alphabetical.
export function dominantVault(c: BrainCluster): string {
  const entries = Object.entries(c.vaults).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return entries[0]?.[0] ?? "";
}

// The first two label words, cut short enough to sit under a node.
export function shortLabel(label: string): string {
  const words = label.split(/\s+/).filter(Boolean).slice(0, 2).join(" ");
  return words.length > 18 ? `${words.slice(0, 17)}…` : words;
}
