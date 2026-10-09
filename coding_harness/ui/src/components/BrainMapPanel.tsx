// Side panel for one topic cluster: label, size, vault mix and its central
// notes, each opening in the Brain reader.

import { TIER_NAMES } from "@/lib/prompt";
import type { BrainCentralNote, BrainCluster } from "@/lib/types";
import { SectionLabel, TextAction } from "./shared";

// Fill colour for one palette token, as CSS (theme-aware through the token).
export function tokenCss(token: string): string {
  return `hsl(var(--${token}))`;
}

function noteName(n: BrainCentralNote): string {
  return n.title.trim() || n.path.split("/").pop()?.replace(/\.md$/, "") || n.path;
}

export function BrainMapPanel({ cluster, notes, hits, palette, onOpen, onClose }: {
  cluster: BrainCluster;
  notes: BrainCentralNote[];
  hits: number;
  palette: Map<string, string>;
  onOpen: (path: string) => void;
  onClose: () => void;
}) {
  const vaults = Object.entries(cluster.vaults).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  return (
    <aside aria-label="topic detail" className="w-72 shrink-0 overflow-y-auto border-l border-rule pl-5">
      <div className="flex items-start gap-2 mb-1">
        <h3 className="font-serif text-xl text-ink leading-tight">{cluster.label}</h3>
        <span className="ml-auto"><TextAction onClick={onClose}>close</TextAction></span>
      </div>
      <p className="font-mono text-xs text-muted mb-4">
        {cluster.size} notes · up to {TIER_NAMES[cluster.max_tier] ?? "restricted"}
        {hits > 0 && <span className="text-accent"> · {hits} hits</span>}
      </p>
      <SectionLabel>vaults</SectionLabel>
      <ul className="space-y-1.5 mb-5" aria-label="vault mix">
        {vaults.map(([vault, n]) => (
          <li key={vault} className="text-xs">
            <div className="flex justify-between font-mono text-muted">
              <span className="truncate">{vault || "(top level)"}</span><span>{n}</span>
            </div>
            <div className="h-1.5 rounded bg-rule/60">
              <div className="h-1.5 rounded" style={{
                width: `${Math.max(4, (100 * n) / cluster.size)}%`,
                background: tokenCss(palette.get(vault) ?? "muted"),
              }} />
            </div>
          </li>
        ))}
      </ul>
      <SectionLabel>central notes</SectionLabel>
      <ul className="space-y-2" aria-label="central notes">
        {notes.map((n) => (
          <li key={n.path} className="flex items-baseline gap-2">
            <span className="min-w-0">
              <span className="block text-sm text-ink truncate">{noteName(n)}</span>
              <span className="block font-mono text-[0.65rem] text-muted truncate">{n.path}</span>
            </span>
            <span className="ml-auto shrink-0">
              <TextAction tone="accent" onClick={() => onOpen(n.path)}>open</TextAction>
            </span>
          </li>
        ))}
      </ul>
    </aside>
  );
}
