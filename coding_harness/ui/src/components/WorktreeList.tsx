// The repo's worktrees with a dot for each one that has a live GUI server.
// "open" asks this server to find or start that worktree's server (only a
// listed path is accepted) and opens it in a new tab with a fresh session.

import { useEffect, useState } from "react";

import { API_BASE } from "@/lib/api";
import { postJson } from "@/lib/git";
import type { WorktreeRow } from "@/lib/types";
import { Button, EmptyState, TextAction } from "./shared";

// Mirrors serve_git._post_worktree: charset and length, no leading . or -,
// no "..", no ".lock" suffix.
export function validTopic(topic: string): boolean {
  return /^[A-Za-z0-9._-]{1,64}$/.test(topic) && !/^[.-]/.test(topic)
    && !topic.includes("..") && !topic.endsWith(".lock");
}

function useWorktrees(nonce: number): { rows: WorktreeRow[] | null; error: string | null } {
  const [state, setState] = useState<{ rows: WorktreeRow[] | null; error: string | null }>(
    { rows: null, error: null });
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch(`${API_BASE}/v1/worktrees`);
        const body = (await res.json()) as { worktrees?: WorktreeRow[]; error?: string };
        if (!cancelled) setState({ rows: body.worktrees ?? [], error: body.error ?? null });
      } catch {
        if (!cancelled) setState({ rows: [], error: "server unreachable" });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [nonce]);
  return state;
}

function Row({ row, onOpen }: { row: WorktreeRow; onOpen: (path: string) => void }) {
  const name = row.path.split("/").slice(-1)[0];
  return (
    <div className="flex items-baseline gap-2 font-mono text-[0.7rem] py-0.5" title={row.path}>
      <span className={`inline-block w-1.5 h-1.5 rounded-full shrink-0 ${row.live ? "bg-accent" : "bg-rule"}`}
        aria-label={row.live ? "server running" : "no server"} />
      <span className="text-ink/90 truncate">{name}</span>
      <span className="text-muted truncate flex-1">
        {row.detached ? "detached" : row.branch ?? ""}{row.locked ? " · locked" : ""}
      </span>
      {row.current ? (
        <span className="label">here</span>
      ) : (
        !row.bare && <TextAction onClick={() => onOpen(row.path)}>open</TextAction>
      )}
    </div>
  );
}

export function WorktreeList() {
  const [nonce, setNonce] = useState(0);
  const { rows, error } = useWorktrees(nonce);
  const [topic, setTopic] = useState("");
  const [note, setNote] = useState<string | null>(null);

  const open = async (path: string) => {
    setNote("starting…");
    const out = await postJson<{ origin: string }>("/v1/worktrees/open", { path });
    if (!out.ok || !out.data) {
      setNote(out.error ?? "could not start it");
      return;
    }
    setNote(null);
    window.open(`${out.data.origin}/?new=1`, "_blank", "noopener");
    setNonce((n) => n + 1);
  };

  const create = async () => {
    const out = await postJson<{ path: string }>("/v1/worktrees", { topic });
    setNote(out.ok ? `created ${out.data?.path.split("/").slice(-1)[0] ?? topic}` : out.error);
    if (out.ok) setTopic("");
    setNonce((n) => n + 1);
  };

  if (rows === null) return <EmptyState>Reading worktrees…</EmptyState>;
  return (
    <div>
      {error && <p className="text-xs text-danger">{error}</p>}
      {rows.map((r) => <Row key={r.path} row={r} onOpen={(p) => void open(p)} />)}
      <div className="flex items-center gap-2 mt-3">
        <input aria-label="new worktree topic" value={topic} placeholder="topic"
          onChange={(e) => setTopic(e.target.value)} className="input py-1 font-mono text-xs flex-1" />
        <Button variant="outline" onClick={() => void create()} disabled={!validTopic(topic)}
          title="git worktree add ../<repo>-<topic> -b <topic>">
          new worktree
        </Button>
      </div>
      {note && <p className="text-xs text-muted italic mt-1">{note}</p>}
    </div>
  );
}
