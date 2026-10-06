// One board card: project, status, pending approvals, the latest reply and
// how long ago the session last did anything.

import type { BoardRow, BoardStatus } from "@/lib/types";

const STATUS_TONE: Record<BoardStatus, string> = {
  needs_approval: "text-accent border-accent/60",
  running: "text-gold border-gold/60",
  error: "text-danger border-danger/60",
  done: "text-ink border-rule",
  idle: "text-muted border-rule",
};

const STATUS_WORD: Record<BoardStatus, string> = {
  needs_approval: "needs approval",
  running: "running",
  error: "error",
  done: "done",
  idle: "idle",
};

// "12s", "4m", "3h" since an epoch-seconds timestamp; empty when unknown.
export function elapsed(ts: number | null, nowMs: number): string {
  if (!ts) return "";
  const s = Math.max(0, Math.round(nowMs / 1000 - ts));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

export function SessionCard({ project, row, nowMs, onOpen }: {
  project: string;
  row: BoardRow;
  nowMs: number;
  onOpen: () => void;
}) {
  const since = elapsed(row.last_event_ts, nowMs);
  return (
    <button type="button" onClick={onOpen} aria-label={`open ${project} ${row.title ?? row.session_id}`}
      className="card text-left p-3 flex flex-col gap-2 min-w-0 hover:border-accent/60">
      <span className="flex items-center gap-2 min-w-0">
        <span className="font-mono text-xs text-ink truncate">{project}</span>
        <span className={`ml-auto shrink-0 font-mono text-[0.65rem] uppercase tracking-[0.12em] border rounded px-1.5 py-0.5 ${STATUS_TONE[row.status]}`}>
          {STATUS_WORD[row.status]}
          {row.pending > 0 ? ` · ${row.pending}` : ""}
        </span>
      </span>
      <span className="text-sm text-ink truncate">{row.title ?? "untitled session"}</span>
      <span className="text-xs text-muted line-clamp-3 whitespace-pre-wrap break-words min-h-[2.5em]">
        {row.last_excerpt || "no output yet"}
      </span>
      <span className="label flex gap-2 flex-wrap">
        <span>{row.model}</span>
        <span>{row.autonomy}</span>
        {since && <span className="ml-auto">{since} ago</span>}
      </span>
    </button>
  );
}
