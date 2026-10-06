// Work: the Beads backlog, grouped by priority — one place to see every
// tracked item instead of hunting across terminal panes. Read-only.

import { useState } from "react";

import { usePoll } from "@/hooks/usePoll";
import type { WorkItem, WorkResponse } from "@/lib/types";
import { EmptyState, SectionLabel } from "./shared";

function PrioritySection({
  label,
  items,
}: {
  label: string;
  items: WorkItem[];
}) {
  if (items.length === 0) return null;
  return (
    <div className="mb-6">
      <SectionLabel>
        {label} · {items.length}
      </SectionLabel>
      <ul className="space-y-1">
        {items.map((i) => (
          <li key={i.id} className="flex items-baseline gap-3 py-0.5">
            <span
              className={`font-mono text-xs shrink-0 ${
                i.priority === 1 ? "text-accent" : "text-muted"
              }`}
            >
              {i.id}
            </span>
            <span className="font-mono text-[0.65rem] text-muted shrink-0 w-12">
              {i.issue_type ?? ""}
            </span>
            <span className="text-sm text-ink/90 min-w-0 truncate" title={i.title}>
              {i.title}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function WorkView() {
  const [status, setStatus] = useState<"open" | "closed">("open");
  const data = usePoll<WorkResponse>(`/v1/work?status=${status}`, 30000);

  const byPriority = (p: number | null) =>
    data?.items.filter((i) => (i.priority ?? 9) === p) ?? [];
  const rest =
    data?.items.filter((i) => ![0, 1, 2, 3].includes(i.priority ?? 9)) ?? [];

  return (
    <section className="flex-1 min-w-0 card p-5">
      <div className="flex items-center gap-4 mb-4">
        <div
          className="inline-flex border border-rule rounded p-0.5 gap-0.5"
          role="group"
          aria-label="work status filter"
        >
          {(["open", "closed"] as const).map((s) => (
            <button
              key={s}
              type="button"
              onClick={() => setStatus(s)}
              className={`px-3 py-1 rounded font-mono text-[0.68rem] uppercase tracking-[0.14em] ${
                status === s
                  ? "bg-paper text-accent"
                  : "text-muted hover:text-ink"
              }`}
            >
              {s}
            </button>
          ))}
        </div>
        {data && (
          <span className="label ml-auto">
            {data.items.length} items ·{" "}
            {data.source === "live"
              ? "live"
              : `backup${data.backup_age ? ` (${data.backup_age} old)` : ""}`}
          </span>
        )}
      </div>
      {!data ? (
        <EmptyState>loading work items…</EmptyState>
      ) : data.items.length === 0 ? (
        <EmptyState>nothing {status} in the tracker.</EmptyState>
      ) : (
        <div className="overflow-y-auto max-h-[70vh]">
          <PrioritySection label="P0 — critical" items={byPriority(0)} />
          <PrioritySection label="P1 — now" items={byPriority(1)} />
          <PrioritySection label="P2 — next" items={byPriority(2)} />
          <PrioritySection label="P3 — later" items={byPriority(3)} />
          <PrioritySection label="unprioritized" items={rest} />
        </div>
      )}
    </section>
  );
}
