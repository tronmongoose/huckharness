// Brain notes pinned to the session. Each renders into every local turn; a
// confidential one keeps the session on local models from the moment it is pinned.

import { apiPost } from "@/lib/api";
import type { PinnedNote, RecallInfo } from "@/lib/types";
import { TierBadge } from "./BrainView";
import { SectionLabel, TextAction } from "./shared";

export function PinnedNotes({ sessionId, pinned, recall = null }: {
  sessionId: string; pinned: PinnedNote[]; recall?: RecallInfo | null;
}) {
  const unpin = (path: string) =>
    void apiPost(`/v1/sessions/${sessionId}/pin`, { path, pinned: false });
  return (
    <div className="mt-6">
      <SectionLabel>pinned notes</SectionLabel>
      {pinned.length === 0 ? (
        <p className="text-xs text-muted">Pin a note from the brain view to keep it in every turn.</p>
      ) : (
        <ul className="space-y-1">
          {pinned.map((n) => (
            <li key={n.path} className="flex items-center gap-2">
              <span className="font-mono text-[0.68rem] text-ink truncate">{n.path}</span>
              <TierBadge tier={n.tier} />
              <span className="ml-auto">
                <TextAction onClick={() => unpin(n.path)} title={`unpin ${n.path}`}>unpin</TextAction>
              </span>
            </li>
          ))}
        </ul>
      )}
      {recall && recall.paths.length > 0 && (
        <p className="text-xs text-muted mt-4">
          last turn recalled {recall.paths.length} note{recall.paths.length === 1 ? "" : "s"}
          {" "}({recall.bytes} bytes): {recall.paths.join(", ")}
        </p>
      )}
    </div>
  );
}
