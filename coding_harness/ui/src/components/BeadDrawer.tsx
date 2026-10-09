// One bead in a side drawer: its text, dependencies and status, with the
// operator's three actions. Close takes two steps and a reason because it is
// the one action the fleet cannot undo by picking the bead up again.

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { sendJson } from "@/lib/send";
import type { BeadDetail, BeadLink, BeadWriteResult } from "@/lib/types";
import { Button, EmptyState, KV, SectionLabel, SpecTable } from "./shared";

function Prose({ label, text }: { label: string; text: string | null }) {
  if (!text) return null;
  return (
    <div className="mb-4">
      <SectionLabel>{label}</SectionLabel>
      <p className="text-sm text-ink/90 whitespace-pre-wrap break-words">{text}</p>
    </div>
  );
}

function Links({ label, links }: { label: string; links: BeadLink[] }) {
  if (links.length === 0) return null;
  return (
    <div className="mb-4">
      <SectionLabel>{label}</SectionLabel>
      <ul className="space-y-0.5">
        {links.map((l) => (
          <li key={`${l.dependency_type}-${l.id}`} className="flex items-baseline gap-2 text-xs">
            <span className="font-mono text-muted shrink-0">{l.id}</span>
            <span className="font-mono text-[0.65rem] text-muted shrink-0">
              {l.dependency_type} · {l.status}
            </span>
            <span className="text-ink/90 truncate" title={l.title ?? ""}>{l.title}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function CloseControl({ busy, onClose }: { busy: boolean; onClose: (reason: string) => void }) {
  const [open, setOpen] = useState(false);
  const [reason, setReason] = useState("");
  if (!open) {
    return <Button variant="outline" onClick={() => setOpen(true)} disabled={busy}>close</Button>;
  }
  return (
    <div className="w-full space-y-2">
      <textarea aria-label="close reason" className="input h-16" placeholder="why it is done"
        value={reason} onChange={(e) => setReason(e.target.value)} />
      <div className="flex gap-2">
        <Button variant="danger" disabled={busy || !reason.trim()}
          onClick={() => onClose(reason.trim())}>confirm close</Button>
        <Button variant="outline" onClick={() => { setOpen(false); setReason(""); }}>cancel</Button>
      </div>
    </div>
  );
}

function Actions({ id, writable, onDone }: { id: string; writable: boolean; onDone: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState("");
  const act = async (action: string, body: object) => {
    setBusy(true);
    const res = await sendJson<BeadWriteResult>("POST", `/v1/beads/${encodeURIComponent(id)}/${action}`, body);
    setBusy(false);
    setError(res.error);
    if (!res.error) {
      if (action === "note") setNote("");
      onDone();
    }
  };
  if (!writable) {
    return <p className="label">read-only: writes are off on this server</p>;
  }
  return (
    <div className="space-y-3 border-t border-rule pt-4">
      <div className="flex flex-wrap gap-2">
        <Button onClick={() => void act("claim", {})} disabled={busy}>claim</Button>
        <CloseControl busy={busy} onClose={(reason) => void act("close", { reason })} />
      </div>
      <div className="space-y-2">
        <textarea aria-label="note" className="input h-16" placeholder="add a note"
          value={note} onChange={(e) => setNote(e.target.value)} />
        <Button variant="outline" disabled={busy || !note.trim()}
          onClick={() => void act("note", { text: note.trim() })}>add note</Button>
      </div>
      {error && <p className="font-mono text-xs text-danger break-words">{error}</p>}
    </div>
  );
}

export function BeadDrawer({ id, writable, onClose, onChanged }: {
  id: string;
  writable: boolean;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [detail, setDetail] = useState<BeadDetail | null | undefined>(undefined);
  const load = useCallback(async () => {
    setDetail(await apiFetch<BeadDetail>(`/v1/beads/${encodeURIComponent(id)}`));
  }, [id]);
  useEffect(() => {
    setDetail(undefined);
    void load();
  }, [load]);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <aside role="dialog" aria-label={`bead ${id}`}
      className="fixed inset-y-0 right-0 z-40 w-full max-w-md bg-card border-l border-rule overflow-y-auto p-5 shadow-xl">
      <div className="flex items-baseline gap-3 mb-4">
        <span className="font-mono text-xs text-muted">{id}</span>
        <button type="button" onClick={onClose} aria-label="close drawer"
          className="ml-auto label hover:text-ink cursor-pointer">esc</button>
      </div>
      {detail === undefined ? (
        <EmptyState>loading…</EmptyState>
      ) : detail === null ? (
        <EmptyState>could not load this bead.</EmptyState>
      ) : (
        <>
          <h2 className="text-base text-ink mb-4">{detail.title}</h2>
          <div className="mb-4">
            <SpecTable>
              <KV k="status" v={detail.status} />
              <KV k="priority" v={detail.priority ?? "none"} />
              <KV k="type" v={detail.issue_type ?? ""} />
              {detail.assignee && <KV k="assignee" v={detail.assignee} />}
              {detail.close_reason && <KV k="closed for" v={detail.close_reason} />}
            </SpecTable>
          </div>
          <Prose label="description" text={detail.description} />
          <Prose label="acceptance" text={detail.acceptance_criteria} />
          <Prose label="notes" text={detail.notes} />
          <Links label="depends on" links={detail.dependencies} />
          <Links label="needed by" links={detail.dependents} />
          <Actions id={id} writable={writable} onDone={() => { void load(); onChanged(); }} />
        </>
      )}
    </aside>
  );
}
