// Brain tab: a topic map of the second brain by default, or the search list;
// either opens a note in the reader, which attaches it to a session.
// The screen shows every tier; what reaches a model is decided server-side,
// and a confidential note keeps its session on local models from then on.

import { useState } from "react";

import { usePoll } from "@/hooks/usePoll";
import { apiFetch } from "@/lib/api";
import { LOCAL_ONLY_TIER, TIER_NAMES } from "@/lib/prompt";
import type { Attachment, BrainHit, BrainPage, BrainStatus } from "@/lib/types";
import { BrainMap } from "./BrainMap";
import { Markdown } from "./Markdown";
import { Button, EmptyState, SectionLabel, TextAction } from "./shared";

export function TierBadge({ tier, label }: { tier: number; label?: string }) {
  const localOnly = tier >= LOCAL_ONLY_TIER;
  return (
    <span
      className={`font-mono text-[0.6rem] uppercase tracking-widest border rounded px-1.5 py-0.5 ${
        localOnly ? "text-accent border-accent/50" : "text-muted border-rule"}`}
      title={localOnly ? "local models only: attaching it keeps the session local" : "any model"}
    >
      {label || TIER_NAMES[tier] || "restricted"}
    </span>
  );
}

const SETUP_INPROCESS = `"brain": {"backend": "inprocess", "agent_id": "bjorn-harness"}`;

const SETUP = `"brain": {
  "backend": "mcp",
  "command": "/path/to/venv/bin/python",
  "args": ["-m", "slos_recall.server"],
  "agent_id": "bjorn-harness"
}`;

function ageText(hours: number | null): string {
  if (hours === null) return "index age unknown";
  return hours < 1 ? "indexed under an hour ago" : `indexed ${Math.round(hours)}h ago`;
}

// Which backend answers and how old its index is. Past 30 hours recall flags
// it; past a week automatic recall stops using it.
function IndexLine({ status }: { status: BrainStatus }) {
  return (
    <p className="text-xs text-muted mb-2 font-mono flex items-center gap-2" aria-label="index status">
      <span>{status.backend ?? "brain"}</span>
      <span>{ageText(status.age_hours)}</span>
      {status.stale && (
        <span className="text-danger border border-danger/50 rounded px-1 uppercase tracking-widest text-[0.6rem]"
          title="the index has not been rebuilt in over 30 hours">stale</span>
      )}
    </p>
  );
}

function Reader({ page, onAttach, onPin, onClose }: {
  page: BrainPage;
  onAttach: (a: Attachment) => void;
  onPin?: (a: Attachment) => void;
  onClose?: () => void;
}) {
  return (
    <article>
      <div className="flex items-center gap-3 mb-4 flex-wrap">
        <h2 className="font-mono text-sm text-ink">{page.path}</h2>
        <TierBadge tier={page.tier} label={page.sensitivity} />
        <span className="ml-auto flex items-center gap-3">
          {onClose && <TextAction onClick={onClose}>close</TextAction>}
          {onPin && (
            <Button variant="outline" onClick={() => onPin({ path: page.path, tier: page.tier })}>
              pin to session
            </Button>
          )}
          <Button onClick={() => onAttach({ path: page.path, tier: page.tier })}>
            attach to session
          </Button>
        </span>
      </div>
      <Markdown text={page.content} />
    </article>
  );
}

export function BrainView({ onAttach, onPin }: {
  onAttach: (a: Attachment) => void;
  // Pins to the open session: the note then rides every local turn.
  onPin?: (a: Attachment) => void;
}) {
  const status = usePoll<BrainStatus>("/v1/brain/status", 60000);
  const [query, setQuery] = useState("");
  const [vault, setVault] = useState<string | null>(null);
  const [hits, setHits] = useState<BrainHit[] | null>(null);
  const [page, setPage] = useState<BrainPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<"map" | "list">("map");

  const search = async (v: string | null = vault) => {
    if (!query.trim()) return;
    setError(null);
    const qs = `q=${encodeURIComponent(query.trim())}${v ? `&vault=${encodeURIComponent(v)}` : ""}`;
    const res = await apiFetch<{ hits: BrainHit[] }>(`/v1/brain/search?${qs}`);
    if (res) setHits(res.hits);
    else setError("search failed: is Ollama running for the embeddings?");
  };
  const open = async (path: string) => {
    setPage(await apiFetch<BrainPage>(`/v1/brain/page?path=${encodeURIComponent(path)}`));
  };

  if (status && !status.configured) {
    return (
      <section className="max-w-2xl">
        <h2 className="font-serif text-2xl mb-3">No second brain configured</h2>
        <p className="text-sm text-muted mb-3">
          Add this block to <code className="font-mono">~/.config/bjorn/settings.json</code> and
          restart <code className="font-mono">bjorn</code>:
        </p>
        <pre className="bg-card/60 border border-rule rounded p-3 font-mono text-xs mb-3">{SETUP_INPROCESS}</pre>
        <p className="text-sm text-muted mb-3">
          That needs slos_recall installed (<code className="font-mono">make brain-install</code>).
          Without it, run the index as an MCP server:
        </p>
        <pre className="bg-card/60 border border-rule rounded p-3 font-mono text-xs">{SETUP}</pre>
      </section>
    );
  }

  const vaults = [...new Set((hits ?? []).map((h) => h.vault).filter(Boolean))];
  const list = (
    <div className="flex-1 min-h-0 flex gap-8">
      <aside className="w-96 shrink-0 flex flex-col min-h-0">
        <form className="flex gap-2 mb-3" onSubmit={(e) => { e.preventDefault(); void search(); }}>
          <input aria-label="search the brain" value={query} placeholder="search your notes"
            onChange={(e) => setQuery(e.target.value)} className="input" />
          <Button onClick={() => void search()} disabled={!query.trim()}>search</Button>
        </form>
        {error && <p className="text-xs text-danger mb-2">{error}</p>}
        {vaults.length > 1 || vault ? (
          <div className="flex flex-wrap gap-2 mb-3">
            {[null, ...vaults].map((v) => (
              <TextAction key={v ?? "all"} tone={vault === v ? "accent" : "default"}
                onClick={() => { setVault(v); void search(v); }}>{v ?? "all"}</TextAction>
            ))}
          </div>
        ) : null}
        {hits === null ? (
          <EmptyState>Search across your vaults, journals and project memory.</EmptyState>
        ) : (
          <>
            <SectionLabel>{hits.length} results</SectionLabel>
            <ul className="space-y-1 overflow-y-auto min-h-0">
              {hits.map((h) => (
                <li key={h.path}>
                  <button type="button" onClick={() => void open(h.path)}
                    className={`text-left w-full rounded px-2 py-1.5 ${
                      page?.path === h.path ? "bg-card" : "hover:bg-card/60"}`}>
                    <span className="flex items-center gap-2">
                      <span className="font-mono text-[0.72rem] text-ink truncate">{h.path}</span>
                      <span className="ml-auto shrink-0"><TierBadge tier={h.tier} label={h.sensitivity} /></span>
                    </span>
                    <span className="text-xs text-muted line-clamp-2">{h.snippet}</span>
                  </button>
                </li>
              ))}
            </ul>
          </>
        )}
      </aside>
      <section className="flex-1 min-w-0 overflow-y-auto pr-2 max-w-4xl">
        {page ? (
          <Reader page={page} onAttach={onAttach} onPin={onPin} />
        ) : (
          <EmptyState>Open a result to read it. Attach it to send it with your next prompt.</EmptyState>
        )}
      </section>
    </div>
  );
  return (
    <div className="flex-1 min-h-0 flex flex-col">
      <div className="flex items-center gap-5 mb-2">
        <span className="flex gap-3" aria-label="brain view">
          {(["map", "list"] as const).map((m) => (
            <TextAction key={m} tone={mode === m ? "accent" : "default"} onClick={() => setMode(m)}>{m}</TextAction>
          ))}
        </span>
        {status && <IndexLine status={status} />}
      </div>
      {status && status.warnings.length > 0 && (
        <ul aria-label="index warnings" className="text-xs text-accent mb-2 space-y-0.5">
          {status.warnings.map((w) => <li key={w}>{w}</li>)}
        </ul>
      )}
      {status && !status.ok && (
        <p className="text-xs text-danger mb-2">index not answering: {status.error}</p>
      )}
      {mode === "list" ? list : (
        <div className="flex-1 min-h-0 flex gap-8">
          <BrainMap onOpen={(p) => void open(p)} />
          {page && (
            <section className="w-[40%] shrink-0 overflow-y-auto pr-2">
              <Reader page={page} onAttach={onAttach} onPin={onPin} onClose={() => setPage(null)} />
            </section>
          )}
        </div>
      )}
    </div>
  );
}
