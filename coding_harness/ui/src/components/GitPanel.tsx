// Git tab: branch, ahead/behind, the three file lists, and a commit of exactly
// the ticked paths. The server stages nothing else and never pushes. Below
// medium autonomy the commit parks on the permission broker; its card shows
// here and in the permissions tab until the operator decides.

import { useCallback, useEffect, useState } from "react";

import { API_BASE } from "@/lib/api";
import { postJson } from "@/lib/git";
import type { GitCommitInfo, GitFile, GitStatus, PendingPermission } from "@/lib/types";
import { PermissionCard } from "./PermissionCard";
import { Button, EmptyState, SectionLabel } from "./shared";
import { WorktreeList } from "./WorktreeList";

const POLL_MS = 4000;

function useGitStatus(refreshKey: string): [GitStatus | null, () => void] {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [nonce, setNonce] = useState(0);
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      try {
        const res = await fetch(`${API_BASE}/v1/git`);
        if (res.ok && !cancelled) setStatus((await res.json()) as GitStatus);
      } catch {
        // fail soft: keep the last good status
      }
    };
    void tick();
    const t = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [refreshKey, nonce]);
  return [status, () => setNonce((n) => n + 1)];
}

function FileList({ title, files, picked, onToggle }: {
  title: string;
  files: GitFile[];
  picked: Set<string>;
  onToggle: (path: string) => void;
}) {
  if (files.length === 0) return null;
  return (
    <div className="mb-3">
      <p className="label mb-1">{title} ({files.length})</p>
      {files.map((f) => (
        <label key={`${title}:${f.path}`} className="flex items-baseline gap-2 font-mono text-[0.7rem] py-0.5 cursor-pointer">
          <input type="checkbox" checked={picked.has(f.path)} onChange={() => onToggle(f.path)}
            aria-label={`${title} ${f.path}`} />
          <span className="text-muted w-3 shrink-0">{f.status}</span>
          <span className="text-ink/90 break-all">{f.orig ? `${f.orig} → ${f.path}` : f.path}</span>
        </label>
      ))}
    </div>
  );
}

// A ticked staged rename also sends its old path, or the delete side stays
// staged and uncommitted.
export function withRenameSources(picked: Set<string>, staged: GitFile[]): string[] {
  const out = [...picked];
  for (const f of staged) {
    if (f.orig && picked.has(f.path) && !out.includes(f.orig)) out.push(f.orig);
  }
  return out;
}

function BranchLine({ status }: { status: GitStatus }) {
  const where = status.detached
    ? `detached at ${(status.head ?? "").slice(0, 8)}`
    : status.branch ?? "(no branch)";
  return (
    <p className="font-mono text-xs mb-3">
      <span className="text-ink">{where}</span>
      {status.upstream && (
        <span className="text-muted"> · {status.upstream} ↑{status.ahead} ↓{status.behind}</span>
      )}
    </p>
  );
}

export function GitPanel({ sessionId, turnInFlight, pending = [], lastCommit = null }: {
  sessionId: string;
  turnInFlight: boolean;
  pending?: PendingPermission[];
  lastCommit?: GitCommitInfo | null;
}) {
  const [status, refresh] = useGitStatus(lastCommit?.sha ?? "");
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);

  const toggle = useCallback((path: string) => {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(path)) next.delete(path);
      else next.add(path);
      return next;
    });
  }, []);

  if (!status) return <EmptyState>Reading git status…</EmptyState>;
  if (status.error) return <EmptyState>Not a git checkout: {status.error}</EmptyState>;

  const commit = async () => {
    setBusy(true);
    setResult(null);
    const out = await postJson<GitCommitInfo>("/v1/git/commit",
      { session_id: sessionId, message, paths: withRenameSources(picked, status.staged) });
    setBusy(false);
    if (out.ok && out.data) {
      setResult(`committed ${out.data.sha.slice(0, 8)}`);
      setPicked(new Set());
      setMessage("");
    } else {
      setResult(out.error);
    }
    refresh();
  };

  const asks = pending.filter((p) => p.tool === "GitCommit");
  const clean = !status.staged.length && !status.unstaged.length && !status.untracked.length;
  const disabled = busy || turnInFlight || picked.size === 0 || !message.trim();
  return (
    <div>
      <BranchLine status={status} />
      {clean && <EmptyState>Working tree clean.</EmptyState>}
      <FileList title="staged" files={status.staged} picked={picked} onToggle={toggle} />
      <FileList title="unstaged" files={status.unstaged} picked={picked} onToggle={toggle} />
      <FileList title="untracked" files={status.untracked} picked={picked} onToggle={toggle} />
      {!clean && (
        <div className="mt-2">
          <textarea aria-label="commit message" value={message} rows={3}
            onChange={(e) => setMessage(e.target.value)}
            className="input w-full font-mono text-xs" placeholder="area: what changed" />
          <div className="flex items-center gap-3 mt-2">
            <Button onClick={() => void commit()} disabled={disabled}
              title={turnInFlight ? "commit after this turn ends" : "commits only the ticked paths"}>
              {busy ? "committing…" : `commit ${picked.size || ""}`.trim()}
            </Button>
            {result && <span className="text-xs text-muted italic">{result}</span>}
          </div>
          <p className="text-[0.65rem] text-muted mt-1">below medium autonomy the commit asks you first. push stays a shell action.</p>
        </div>
      )}
      {asks.map((req) => (
        <PermissionCard key={req.req_id} sessionId={sessionId} request={req} />
      ))}
      <div className="mt-6">
        <SectionLabel>worktrees</SectionLabel>
        <WorktreeList />
      </div>
    </div>
  );
}
