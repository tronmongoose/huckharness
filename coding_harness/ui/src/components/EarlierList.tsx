// Past sessions started in this project. Opening one resumes it on the
// server, which replays its transcript into the thread. Search, rename and
// delete work in place; a session that cannot resume says why before a click.

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { sendJson } from "@/lib/send";
import type { TranscriptInfo } from "@/lib/types";
import { SectionLabel } from "./shared";

const POLL_MS = 15000;
const DEBOUNCE_MS = 250;

const REASONS: Record<string, string> = {
  envelope_expired: "envelope expired",
  transcript_incomplete: "incomplete transcript",
  other_project: "another project",
};

function ago(iso: string): string {
  const mins = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (Number.isNaN(mins)) return "";
  if (mins < 60) return `${mins}m`;
  if (mins < 60 * 24) return `${Math.round(mins / 60)}h`;
  return `${Math.round(mins / 1440)}d`;
}

// The list for a query, refetched on an interval and on demand.
function useHistory(query: string): [TranscriptInfo[] | null, () => void] {
  const [items, setItems] = useState<TranscriptInfo[] | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const path = query ? `/v1/transcripts?q=${encodeURIComponent(query)}` : "/v1/transcripts";
    const tick = async () => {
      const res = await apiFetch<{ transcripts: TranscriptInfo[] }>(path);
      if (!cancelled && res !== null) setItems(res.transcripts ?? []);
    };
    void tick();
    const t = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [query, nonce]);
  return [items, useCallback(() => setNonce((n) => n + 1), [])];
}

function useDebounced(value: string, ms: number): string {
  const [out, setOut] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setOut(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return out;
}

function Row({ t, onOpen, onChanged, onError }: {
  t: TranscriptInfo;
  onOpen: (t: TranscriptInfo) => void;
  onChanged: () => void;
  onError: (msg: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(t.title);
  const [confirming, setConfirming] = useState(false);
  const reason = t.resumable === false ? REASONS[t.reason_if_not ?? ""] ?? t.reason_if_not : null;

  const save = async () => {
    const { error } = await sendJson("POST", `/v1/transcripts/${t.id}/title`, { title: draft });
    if (error) { onError(`rename failed: ${error}`); return; }
    setEditing(false);
    onChanged();
  };
  const remove = async () => {
    const { error } = await sendJson("POST", `/v1/transcripts/${t.id}/delete`, {});
    setConfirming(false);
    if (error) onError(`delete failed: ${error}`);
    onChanged();
  };

  if (editing) {
    return (
      <input aria-label="session title" autoFocus value={draft} maxLength={120}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={() => setEditing(false)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void save();
          if (e.key === "Escape") setEditing(false);
        }}
        className="w-full rounded px-2 py-1 text-sm bg-card border border-rule" />
    );
  }
  return (
    <div className="group flex items-start gap-1 rounded hover:bg-card/60">
      <button type="button" disabled={!!reason} onClick={() => onOpen(t)}
        title={reason ? `cannot resume: ${reason}` : t.first_prompt ?? t.title}
        className="text-left flex-1 min-w-0 px-2 py-1 text-muted hover:text-ink disabled:hover:text-muted disabled:cursor-default">
        <span className="text-sm truncate block">{t.title}</span>
        <span className="font-mono text-[0.6rem] block">
          {ago(t.started ?? t.modified)} · {t.turns ?? 0} {t.turns === 1 ? "turn" : "turns"}
          {reason && <span className="text-danger"> · {reason}</span>}
        </span>
      </button>
      {confirming ? (
        <span className="font-mono text-[0.6rem] pt-1.5 pr-1 shrink-0 flex gap-1.5">
          delete?
          <button type="button" className="text-danger underline" onClick={() => void remove()}>yes</button>
          <button type="button" className="underline" onClick={() => setConfirming(false)}>no</button>
        </span>
      ) : (
        <span className="shrink-0 pt-1 pr-1 flex gap-1 opacity-60 group-hover:opacity-100">
          <button type="button" aria-label={`rename ${t.title}`} title="rename"
            className="text-xs text-muted hover:text-ink" onClick={() => { setDraft(t.title); setEditing(true); }}>
            ✎
          </button>
          <button type="button" aria-label={`delete ${t.title}`} title="delete"
            className="text-xs text-muted hover:text-danger" onClick={() => setConfirming(true)}>
            ×
          </button>
        </span>
      )}
    </div>
  );
}

export function EarlierList({ onOpened }: { onOpened: (id: string) => void }) {
  const [query, setQuery] = useState("");
  const debounced = useDebounced(query.trim(), DEBOUNCE_MS);
  const [items, refresh] = useHistory(debounced);
  const [error, setError] = useState<string | null>(null);
  if (items === null || (items.length === 0 && !query && !debounced)) return null;

  const open = async (t: TranscriptInfo) => {
    setError(null);
    const { error: problem } = await sendJson("POST", `/v1/sessions/${t.id}/resume`, { interactive: true });
    if (problem) setError(`could not resume: ${problem}`);
    else onOpened(t.id);
  };

  return (
    <div className="min-h-0 flex flex-col">
      <SectionLabel>Earlier</SectionLabel>
      <input type="search" aria-label="search history" placeholder="search earlier sessions"
        value={query} onChange={(e) => setQuery(e.target.value)}
        className="mb-1 w-full rounded px-2 py-1 text-xs bg-transparent border border-rule" />
      {error && <p className="text-xs text-danger mb-1">{error}</p>}
      {items.length === 0 && <p className="text-xs italic text-muted px-2">no matches</p>}
      <ul className="space-y-0.5 overflow-y-auto min-h-0 max-h-[30vh]">
        {items.map((t) => (
          <li key={t.id}>
            <Row t={t} onOpen={(x) => void open(x)} onChanged={refresh}
              onError={(m) => setError(m)} />
          </li>
        ))}
      </ul>
    </div>
  );
}
