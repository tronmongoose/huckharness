// The skill buckets the model's skills fall into, from the same index as its
// system prompt. Clicking one opens the Skills tab at that bucket.

import { usePoll } from "@/hooks/usePoll";
import type { SkillsResponse } from "@/lib/types";
import { EmptyState, SectionLabel } from "./shared";

export function SkillsList({ onOpen }: { onOpen: (bucket: string) => void }) {
  const res = usePoll<SkillsResponse>("/v1/skills", 60000);
  const total = res?.skills.length ?? 0;
  const buckets = (res?.buckets ?? []).filter((b) => b.count > 0);
  return (
    <div className="min-h-0 flex flex-col">
      <SectionLabel>Skills · {total}</SectionLabel>
      {res && total === 0 ? (
        <EmptyState>none found in ~/.config/bjorn/skills or ~/.claude/skills</EmptyState>
      ) : (
        <ul className="space-y-0.5 overflow-y-auto min-h-0">
          {buckets.map((b) => (
            <li key={b.name}>
              <button type="button" onClick={() => onOpen(b.name)}
                className="flex items-baseline gap-2 text-left w-full rounded px-2 py-1 text-muted hover:text-ink hover:bg-card/60">
                <span className="text-sm truncate">{b.name}</span>
                <span className="ml-auto font-mono text-[0.7rem] tabular-nums">{b.count}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
