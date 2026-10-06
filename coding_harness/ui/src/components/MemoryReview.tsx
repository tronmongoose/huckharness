// Memory tab: proposals a local model drafted after a turn. Nothing reaches the
// memory directory until the operator approves; approve runs the same
// PreToolUse hooks a model's Write would meet, so a schema gate can refuse it.
// Every decision echoes the sha256 the listing gave for the text this card
// shows, so a stale card is refused (409) instead of deciding newer text.

import { useEffect, useState } from "react";

import { API_BASE, apiFetch, notifyUnauthorized } from "@/lib/api";
import type { MemoryProposal } from "@/lib/types";
import { Button, EmptyState, TextAction } from "./shared";

type Decision = "approve" | "edit" | "reject";

interface Listing {
  proposals: MemoryProposal[];
}

type Shown = Record<string, { text: string; sha256: string }>;

/** POST a decision; the server's error message on refusal, else null. */
export async function decideMemory(
  sessionId: string, id: string, decision: Decision, sha256: string, text?: string,
): Promise<string | null> {
  const body = text === undefined ? { id, decision, sha256 } : { id, decision, sha256, text };
  try {
    const res = await fetch(`${API_BASE}/v1/sessions/${sessionId}/memories`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (res.status === 401) notifyUnauthorized();
    if (res.ok) return null;
    const err = (await res.json().catch(() => null)) as { error?: { message?: string } } | null;
    return err?.error?.message ?? `failed (${res.status})`;
  } catch {
    return "server unreachable";
  }
}

function ProposalCard({ sessionId, row, onDone, onStale }: {
  sessionId: string; row: MemoryProposal; onDone: (id: string) => void; onStale: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(row.text ?? "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const ready = Boolean(row.text && row.sha256);
  useEffect(() => { if (!editing) setText(row.text ?? ""); }, [row.text, editing]);

  const decide = async (decision: Decision) => {
    if (!row.sha256) return;
    setBusy(true);
    const problem = await decideMemory(sessionId, row.id, decision, row.sha256,
      decision === "edit" ? text : undefined);
    setBusy(false);
    if (problem) {
      setError(problem);
      onStale();
    } else onDone(row.id);
  };

  return (
    <article className="border border-rule rounded p-3 mb-3" aria-label={`proposal ${row.name}`}>
      <div className="flex items-baseline gap-2 mb-1">
        <span className="font-mono text-[0.72rem] text-ink">{row.name}</span>
        <span className="label ml-auto">{row.type}</span>
      </div>
      <p className="text-xs text-muted mb-2">{row.description}</p>
      {editing ? (
        <textarea aria-label={`edit ${row.name}`} value={text} rows={10}
          onChange={(e) => setText(e.target.value)}
          className="input font-mono text-[0.7rem] block mb-2" />
      ) : (
        row.text && (
          <pre className="font-mono text-[0.66rem] text-ink/90 whitespace-pre-wrap max-h-48 overflow-y-auto mb-2">
            {row.text}
          </pre>
        )
      )}
      {error && <p className="text-xs text-danger mb-2">{error}</p>}
      <div className="flex items-center gap-4">
        {editing ? (
          <>
            <Button onClick={() => void decide("edit")} disabled={busy || !ready || !text.trim()}>save and approve</Button>
            <TextAction onClick={() => setEditing(false)}>cancel</TextAction>
          </>
        ) : (
          <>
            <Button onClick={() => void decide("approve")} disabled={busy || !ready}>approve</Button>
            <TextAction onClick={() => setEditing(true)} disabled={busy || !ready}>edit</TextAction>
            <TextAction tone="danger" onClick={() => void decide("reject")} disabled={busy || !ready}>reject</TextAction>
          </>
        )}
      </div>
    </article>
  );
}

export function MemoryReview({ sessionId, proposals }: {
  sessionId: string; proposals: MemoryProposal[];
}) {
  const [shown, setShown] = useState<Shown>({});
  const [reloads, setReloads] = useState(0);
  const [done, setDone] = useState<ReadonlySet<string>>(() => new Set());
  const key = proposals.map((p) => p.id).join(",");
  useEffect(() => {
    if (!key) return;
    let live = true;
    void apiFetch<Listing>(`/v1/sessions/${sessionId}/memories`).then((res) => {
      if (!live || !res) return;
      setShown(Object.fromEntries(res.proposals.map((p) =>
        [p.id, { text: p.text ?? "", sha256: p.sha256 ?? "" }])));
    });
    return () => { live = false; };
  }, [sessionId, key, reloads]);
  // Keep the hidden set to ids the stream still lists.
  useEffect(() => {
    setDone((d) => new Set([...d].filter((id) => proposals.some((p) => p.id === id))));
  }, [proposals]);

  const rows = proposals.filter((p) => !done.has(p.id));
  if (rows.length === 0) {
    return (
      <EmptyState>
        No proposals. After a turn with a correction, a denial, a fixed check or a
        brain hit, a local model may draft memories here for you to review.
      </EmptyState>
    );
  }
  return (
    <div>
      {rows.map((row) => (
        <ProposalCard key={row.id} sessionId={sessionId}
          row={{ ...row, text: shown[row.id]?.text, sha256: shown[row.id]?.sha256 }}
          onStale={() => setReloads((n) => n + 1)}
          onDone={(id) => setDone((d) => new Set([...d, id]))} />
      ))}
    </div>
  );
}
