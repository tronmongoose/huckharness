// Notes queued to go out with the next prompt. A confidential or higher note
// says what attaching it does: the session stays on local models from then on.

import { LOCAL_ONLY_TIER } from "@/lib/prompt";
import type { Attachment } from "@/lib/types";
import { TierBadge } from "./BrainView";

export function AttachChips({ items, onRemove }: {
  items: Attachment[]; onRemove: (path: string) => void;
}) {
  if (items.length === 0) return null;
  const pins = items.some((a) => a.tier >= LOCAL_ONLY_TIER);
  return (
    <div className="flex flex-wrap items-center gap-2 pb-2">
      {items.map((a) => (
        <span key={a.path} className="flex items-center gap-1.5 border border-rule rounded px-2 py-0.5">
          <span className="font-mono text-[0.68rem] text-ink">{a.path}</span>
          <TierBadge tier={a.tier} />
          <button type="button" aria-label={`remove ${a.path}`} onClick={() => onRemove(a.path)}
            className="text-muted hover:text-danger text-xs">×</button>
        </span>
      ))}
      {pins && (
        <span className="text-xs text-accent italic">
          sending this keeps the session on local models
        </span>
      )}
    </div>
  );
}
