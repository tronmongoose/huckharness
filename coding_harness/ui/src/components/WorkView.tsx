// Work: the beads backlog grouped by priority. A row opens the bead in a
// drawer where the operator can claim, close or note it; the form on top
// files a new one. The server says when it has no beads directory, and why.

import { useCallback, useEffect, useMemo, useState } from "react";

import { apiFetch } from "@/lib/api";
import { sendJson } from "@/lib/send";
import type { BeadType, BeadWriteResult, WorkItem, WorkResponse } from "@/lib/types";
import { BeadDrawer } from "./BeadDrawer";
import { Button, EmptyState, SectionLabel } from "./shared";

const TYPES: BeadType[] = ["task", "bug", "feature", "chore", "epic", "decision"];
const GROUPS: [string, (p: number | null) => boolean][] = [
  ["P0 — critical", (p) => p === 0],
  ["P1 — now", (p) => p === 1],
  ["P2 — next", (p) => p === 2],
  ["P3 — later", (p) => p === 3],
  ["P4 and unprioritized", (p) => p === null || p > 3],
];

/** The work list for one status, polled, with a reload for after a write. */
function useWork(status: string): [WorkResponse | null, () => void] {
  const [data, setData] = useState<WorkResponse | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      const res = await apiFetch<WorkResponse>(`/v1/work?status=${status}`);
      if (!cancelled && res !== null) setData(res);
    };
    void load();
    const t = setInterval(load, 30000);
    return () => { cancelled = true; clearInterval(t); };
  }, [status, tick]);
  useEffect(() => setData(null), [status]);
  return [data, useCallback(() => setTick((n) => n + 1), [])];
}

function NewBead({ onCreated }: { onCreated: (id: string | null) => void }) {
  const [title, setTitle] = useState("");
  const [priority, setPriority] = useState(2);
  const [type, setType] = useState<BeadType>("task");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    const res = await sendJson<BeadWriteResult>("POST", "/v1/beads", { title: title.trim(), priority, type });
    setBusy(false);
    setError(res.error);
    if (!res.error) { setTitle(""); onCreated(res.data?.id ?? null); }
  };
  return (
    <form className="mb-4" onSubmit={(e) => { e.preventDefault(); if (title.trim()) void submit(); }}>
      <div className="flex gap-2 items-center">
        <input aria-label="new bead title" className="input flex-1" placeholder="new bead title"
          value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} />
        <select aria-label="new bead priority" className="input w-auto" value={priority}
          onChange={(e) => setPriority(Number(e.target.value))}>
          {[0, 1, 2, 3, 4].map((p) => <option key={p} value={p}>P{p}</option>)}
        </select>
        <select aria-label="new bead type" className="input w-auto" value={type}
          onChange={(e) => setType(e.target.value as BeadType)}>
          {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <Button onClick={() => void submit()} disabled={busy || !title.trim()}>file</Button>
      </div>
      {error && <p className="font-mono text-xs text-danger mt-1 break-words">{error}</p>}
    </form>
  );
}

function PrioritySection({ label, items, onOpen }: {
  label: string;
  items: WorkItem[];
  onOpen: (id: string) => void;
}) {
  if (items.length === 0) return null;
  return (
    <div className="mb-6">
      <SectionLabel>{label} · {items.length}</SectionLabel>
      <ul>
        {items.map((i) => (
          <li key={i.id}>
            <button type="button" onClick={() => onOpen(i.id)}
              className="w-full flex items-baseline gap-3 px-1.5 py-1 rounded text-left cursor-pointer hover:bg-paper focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40">
              <span className="font-mono text-xs shrink-0 w-24 truncate text-ink/80" title={i.id}>{i.id}</span>
              <span className="font-mono text-[0.65rem] text-muted shrink-0 w-14">{i.issue_type ?? ""}</span>
              <span className="text-sm text-ink/90 min-w-0 truncate" title={i.title}>{i.title}</span>
              {i.status === "in_progress" && <span className="label ml-auto shrink-0">claimed</span>}
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

function StatusToggle({ status, setStatus }: { status: string; setStatus: (s: "open" | "closed") => void }) {
  return (
    <div className="inline-flex border border-rule rounded p-0.5 gap-0.5" role="group" aria-label="work status filter">
      {(["open", "closed"] as const).map((s) => (
        <button key={s} type="button" onClick={() => setStatus(s)}
          className={`px-3 py-1 rounded font-mono text-[0.68rem] uppercase tracking-[0.14em] ${
            status === s ? "bg-paper text-accent" : "text-muted hover:text-ink"}`}>
          {s}
        </button>
      ))}
    </div>
  );
}

function sourceLabel(d: WorkResponse): string {
  if (d.source === "live") return "live";
  if (d.source === "none") return "no tracker";
  return `backup${d.backup_age ? ` (${d.backup_age} old)` : ""}`;
}

export function WorkView() {
  const [status, setStatus] = useState<"open" | "closed">("open");
  const [type, setType] = useState("");
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState<string | null>(null);
  const [data, reload] = useWork(status);
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (data?.items ?? []).filter((i) =>
      (!type || i.issue_type === type) &&
      (!q || i.id.toLowerCase().includes(q) || (i.title ?? "").toLowerCase().includes(q)));
  }, [data, type, query]);

  return (
    <section className="flex-1 min-w-0 card p-5">
      {data?.writable && <NewBead onCreated={(id) => { reload(); if (id) setOpen(id); }} />}
      <div className="flex flex-wrap items-center gap-3 mb-4">
        <StatusToggle status={status} setStatus={setStatus} />
        <select aria-label="type filter" className="input w-auto" value={type} onChange={(e) => setType(e.target.value)}>
          <option value="">all types</option>
          {TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
        </select>
        <input aria-label="search work" className="input w-48" placeholder="search id or title"
          value={query} onChange={(e) => setQuery(e.target.value)} />
        {data && (
          <span className="label ml-auto">
            {shown.length} of {data.items.length} · {sourceLabel(data)}
            {data.source !== "none" && !data.writable && " · read-only"}
          </span>
        )}
      </div>
      {!data ? (
        <EmptyState>loading work items…</EmptyState>
      ) : data.source === "none" ? (
        <EmptyState>{data.reason}</EmptyState>
      ) : shown.length === 0 ? (
        <EmptyState>nothing {status} matches.</EmptyState>
      ) : (
        <div className="overflow-y-auto max-h-[70vh]">
          {GROUPS.map(([label, match]) => (
            <PrioritySection key={label} label={label} onOpen={setOpen}
              items={shown.filter((i) => match(i.priority))} />
          ))}
        </div>
      )}
      {open && <BeadDrawer id={open} writable={!!data?.writable}
        onClose={() => setOpen(null)} onChanged={reload} />}
    </section>
  );
}
