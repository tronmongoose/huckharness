// Brain topic map: the index's clusters as a force-laid graph on a canvas,
// like Obsidian's graph view but one node per topic. Screen only: the server
// never puts the map or its search into a session or a model's context.
// Under prefers-reduced-motion the layout settles off-screen and draws once.

import { type FormEvent, type MouseEvent, useEffect, useMemo, useRef, useState } from "react";

import { apiFetch } from "@/lib/api";
import {
  dominantVault, edgesFor, initNodes, type LayoutNode, MAX_TICKS, nodeAt, shortLabel, step,
} from "@/lib/brainLayout";
import { TIER_NAMES } from "@/lib/prompt";
import type { BrainCluster, BrainMapHits, BrainMapResponse } from "@/lib/types";
import { BrainMapPanel, tokenCss } from "./BrainMapPanel";
import { Button, EmptyState, TextAction } from "./shared";

// Vault colours come from the theme tokens, assigned in vault-name order.
const PALETTE = ["accent", "gold", "add", "ink", "muted", "danger"];
const FALLBACK = { w: 800, h: 520 };

export function vaultPalette(clusters: BrainCluster[]): Map<string, string> {
  const names = [...new Set(clusters.flatMap((c) => Object.keys(c.vaults)))].sort();
  return new Map(names.map((v, i) => [v, PALETTE[i % PALETTE.length]]));
}

function tokenColor(token: string, alpha: number): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(`--${token}`).trim();
  return v ? `hsl(${v} / ${alpha})` : `rgba(128, 128, 128, ${alpha})`;
}

function reducedMotion(): boolean {
  return typeof window.matchMedia !== "function"
    || window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

interface View {
  clusters: Map<number, BrainCluster>;
  edges: ReturnType<typeof edgesFor>;
  palette: Map<string, string>;
  hits: Record<string, number> | null;
  selected: number | null;
  size: { w: number; h: number };
}

// LONG-FN: one pass over edges then nodes; splitting it scatters the canvas state.
function drawMap(ctx: CanvasRenderingContext2D, nodes: LayoutNode[], v: View): void {
  const { w, h } = v.size;
  const dpr = window.devicePixelRatio || 1;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const maxHit = v.hits ? Math.max(1, ...Object.values(v.hits)) : 1;
  ctx.strokeStyle = tokenColor("muted", v.hits ? 0.12 : 0.35);
  for (const [a, b, sim] of v.edges) {
    const na = byId.get(a);
    const nb = byId.get(b);
    if (!na || !nb) continue;
    ctx.lineWidth = 0.5 + Math.max(0, sim - 0.5) * 3;
    ctx.beginPath();
    ctx.moveTo(na.x, na.y);
    ctx.lineTo(nb.x, nb.y);
    ctx.stroke();
  }
  ctx.font = "10px 'JetBrains Mono Variable', ui-monospace, monospace";
  ctx.textAlign = "center";
  for (const n of nodes) {
    const c = v.clusters.get(n.id);
    if (!c) continue;
    const hit = v.hits?.[String(n.id)] ?? 0;
    const r = n.r * (hit ? 1 + (0.35 * hit) / maxHit : 1);
    ctx.globalAlpha = !v.hits || hit ? 1 : 0.18;
    ctx.shadowBlur = hit ? 8 + (18 * hit) / maxHit : 0;
    ctx.shadowColor = tokenColor("accent", 0.9);
    ctx.fillStyle = tokenColor(v.palette.get(dominantVault(c)) ?? "muted", 0.85);
    ctx.beginPath();
    ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
    ctx.fill();
    ctx.shadowBlur = 0;
    if (v.selected === n.id) {
      ctx.strokeStyle = tokenColor("ink", 0.9);
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(n.x, n.y, r + 4, 0, Math.PI * 2);
      ctx.stroke();
    }
    ctx.fillStyle = tokenColor("ink", 0.9);
    ctx.fillText(shortLabel(c.label), n.x, n.y + r + 12);
  }
  ctx.globalAlpha = 1;
}

export function BrainMap({ onOpen }: { onOpen: (path: string) => void }) {
  const [tier, setTier] = useState(3);
  const [data, setData] = useState<BrainMapResponse | null>(null);
  const [failed, setFailed] = useState(false);
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Record<string, number> | null>(null);
  const [notes, setNotes] = useState<string[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [hover, setHover] = useState<{ id: number; x: number; y: number } | null>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const nodesRef = useRef<LayoutNode[]>([]);

  useEffect(() => {
    let live = true;
    setFailed(false);
    void apiFetch<BrainMapResponse>(`/v1/brain/map?max_tier=${tier}`).then((res) => {
      if (!live) return;
      setData(res);
      setFailed(res === null);
      setHits(null);
      setSelected(null);
    });
    return () => { live = false; };
  }, [tier]);

  const clusters = useMemo(() => (data?.available ? data.clusters : []), [data]);
  const edges = useMemo(() => edgesFor(clusters), [clusters]);
  const palette = useMemo(() => vaultPalette(clusters), [clusters]);
  const byId = useMemo(() => new Map(clusters.map((c) => [c.id, c])), [clusters]);
  const view = useRef<View>({ clusters: byId, edges, palette, hits, selected, size: FALLBACK });
  view.current = { ...view.current, clusters: byId, edges, palette, hits, selected };

  const redraw = () => {
    const ctx = canvasRef.current?.getContext("2d");
    if (ctx) drawMap(ctx, nodesRef.current, view.current);
  };

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || clusters.length === 0) return;
    const w = wrapRef.current?.clientWidth || FALLBACK.w;
    const h = wrapRef.current?.clientHeight || FALLBACK.h;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr;
    canvas.height = h * dpr;
    view.current.size = { w, h };
    const nodes = initNodes(clusters, w, h);
    nodesRef.current = nodes;
    let tick = 0;
    if (reducedMotion() || typeof requestAnimationFrame !== "function") {
      for (; tick < MAX_TICKS; tick++) step(nodes, edges, w, h, tick);
      redraw();
      return;
    }
    let frame = 0;
    const run = () => {
      step(nodes, edges, w, h, tick++);
      redraw();
      if (tick < MAX_TICKS) frame = requestAnimationFrame(run);
    };
    frame = requestAnimationFrame(run);
    return () => cancelAnimationFrame(frame);
  }, [clusters, edges]);

  useEffect(redraw, [hits, selected, palette]);

  const pointAt = (e: MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    return { node: nodeAt(nodesRef.current, x, y), x, y };
  };

  const search = async (e?: FormEvent) => {
    e?.preventDefault();
    const q = query.trim();
    if (!q) return;
    const res = await apiFetch<BrainMapHits>(
      `/v1/brain/map/search?q=${encodeURIComponent(q)}&max_tier=${tier}`);
    setHits(res?.available ? res.hits ?? {} : {});
    setNotes(res === null ? ["topic search failed: is Ollama running for the embeddings?"]
      : res.available ? res.warnings ?? [] : [res.reason ?? "topic search unavailable"]);
  };
  const clear = () => { setHits(null); setNotes([]); setQuery(""); };

  const chosen = selected === null ? undefined : byId.get(selected);
  const hovered = hover ? byId.get(hover.id) : undefined;
  const hitCount = hits ? Object.values(hits).filter((n) => n > 0).length : 0;
  return (
    <div className="flex-1 min-h-0 min-w-0 flex flex-col">
      <form className="flex gap-2 mb-2 items-center" onSubmit={(e) => void search(e)}>
        <input aria-label="search topics" value={query} placeholder="light up topics"
          onChange={(e) => setQuery(e.target.value)} className="input max-w-sm" />
        <Button onClick={() => void search()} disabled={!query.trim()}>search</Button>
        {hits && <TextAction onClick={clear}>clear</TextAction>}
        <label className="ml-auto label flex items-center gap-2">
          up to
          <select aria-label="highest tier shown" value={tier}
            onChange={(e) => setTier(Number(e.target.value))}
            className="bg-transparent border border-rule rounded px-1 py-0.5 font-mono text-xs text-ink">
            {TIER_NAMES.map((name, i) => <option key={name} value={i}>{name}</option>)}
          </select>
        </label>
      </form>
      <MapNotes data={data} tier={tier} hits={hits} hitCount={hitCount} notes={notes} failed={failed}
        palette={palette} />
      {data?.available && clusters.length > 0 && (
        <div className="flex-1 min-h-0 flex gap-4">
          <div ref={wrapRef} className="relative flex-1 min-h-[320px] min-w-0">
            <canvas ref={canvasRef} aria-label="topic map" className="absolute inset-0 w-full h-full"
              style={{ cursor: hover ? "pointer" : "default" }}
              onMouseMove={(e) => {
                const { node, x, y } = pointAt(e);
                setHover(node ? { id: node.id, x, y } : null);
              }}
              onMouseLeave={() => setHover(null)}
              onClick={(e) => setSelected(pointAt(e).node?.id ?? null)} />
            {hover && hovered && (
              <div role="tooltip" className="absolute pointer-events-none bg-card border border-rule rounded px-2 py-1 text-xs shadow-sm"
                style={{ left: hover.x + 12, top: hover.y + 12 }}>
                <span className="text-ink">{hovered.label}</span>
                <span className="font-mono text-muted"> · {hovered.size} notes</span>
              </div>
            )}
            <ul aria-label="topics" className="sr-only">
              {clusters.map((c) => {
                const hit = hits?.[String(c.id)] ?? 0;
                return (
                  <li key={c.id}>
                    <button type="button" data-hit={hit} aria-pressed={selected === c.id}
                      onClick={() => setSelected(c.id)}>
                      {`${c.label}, ${c.size} notes${hit ? `, ${hit} hits` : ""}`}
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
          {chosen && data.available && (
            <BrainMapPanel cluster={chosen} notes={data.central[String(chosen.id)] ?? []}
              hits={hits?.[String(chosen.id)] ?? 0} palette={palette} onOpen={onOpen}
              onClose={() => setSelected(null)} />
          )}
        </div>
      )}
    </div>
  );
}

function MapNotes({ data, tier, hits, hitCount, notes, failed, palette }: {
  data: BrainMapResponse | null; tier: number; hits: Record<string, number> | null;
  hitCount: number; notes: string[]; failed: boolean; palette: Map<string, string>;
}) {
  if (failed) return <p className="text-xs text-danger mb-2">the topic map did not load</p>;
  if (!data) return <EmptyState>Laying out your topics…</EmptyState>;
  if (!data.available) {
    return <EmptyState>No topic map here: {data.reason}. The list view still searches.</EmptyState>;
  }
  if (data.clusters.length === 0) return <EmptyState>No notes are visible at this tier.</EmptyState>;
  return (
    <div className="mb-2 space-y-1">
      <p className="font-mono text-xs text-muted flex flex-wrap gap-x-4 gap-y-1 items-center">
        <span>{data.clusters.length} topics · {data.pages} notes</span>
        {data.max_tier < tier && <span>capped at {TIER_NAMES[data.max_tier]} by the brain identity</span>}
        {hits && <span className="text-accent" aria-label="topic hits">{hitCount} topics match</span>}
        {[...palette].map(([vault, token]) => (
          <span key={vault} className="flex items-center gap-1">
            <span className="inline-block w-2 h-2 rounded-full" style={{ background: tokenCss(token) }} />
            {vault || "(top level)"}
          </span>
        ))}
      </p>
      {notes.map((n) => <p key={n} className="text-xs text-accent">{n}</p>)}
    </div>
  );
}
