// Hand-rolled unified-diff view: per-line tone from the first character, one
// block per file with its path and status, and the accept / reject controls
// the changes tab uses. The review card reuses DiffLines on its own.

import { useState } from "react";

import type { FileChange } from "@/lib/types";
import { Button, TextAction } from "./shared";

// Order matters: file headers start with the same characters as edits.
export function lineClass(line: string): string {
  if (line.startsWith("@@")) return "diff-hunk text-accent/80";
  if (line.startsWith("+++") || line.startsWith("---")) return "diff-meta text-muted";
  if (line.startsWith("+")) return "diff-add text-gold bg-gold/10";
  if (line.startsWith("-")) return "diff-del text-danger bg-danger/10";
  return "diff-ctx text-ink/80";
}

export function DiffLines({ diff }: { diff: string }) {
  if (!diff) return <p className="text-xs italic text-muted py-1">no text diff</p>;
  return (
    <pre className="font-mono text-[0.68rem] leading-snug overflow-x-auto max-h-96 border border-rule rounded py-1">
      {diff.split("\n").map((line, i) => (
        <div key={i} className={`px-2 whitespace-pre ${lineClass(line)}`}>
          {line || " "}
        </div>
      ))}
    </pre>
  );
}

function RejectControl({ onReject, busy }: { onReject: () => void; busy: boolean }) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <Button variant="danger" onClick={() => setConfirming(true)} disabled={busy}>
        reject
      </Button>
    );
  }
  return (
    <span className="flex items-center gap-3">
      <span className="text-xs italic text-muted">put this file back?</span>
      <Button
        variant="danger"
        onClick={() => {
          setConfirming(false);
          onReject();
        }}
        disabled={busy}
      >
        confirm reject
      </Button>
      <TextAction onClick={() => setConfirming(false)}>keep</TextAction>
    </span>
  );
}

export function DiffView({
  file,
  accepted = false,
  reverted = false,
  busy = false,
  onAccept,
  onReject,
}: {
  file: FileChange;
  accepted?: boolean;
  reverted?: boolean;
  busy?: boolean;
  onAccept?: () => void;
  onReject?: () => void;
}) {
  const settled = accepted || reverted;
  return (
    <div className="py-3" data-testid={`diff-${file.path}`}>
      <div className="flex items-baseline gap-3 mb-2">
        <span className="font-mono text-xs text-ink break-all flex-1">{file.path}</span>
        <span className="label">{file.status}</span>
      </div>
      <DiffLines diff={file.diff} />
      {file.truncated && <p className="text-xs italic text-muted mt-1">diff truncated</p>}
      <div className="flex items-center gap-3 mt-2 min-h-7">
        {reverted && <span className="label text-danger">rejected</span>}
        {accepted && !reverted && <span className="label text-gold">accepted</span>}
        {!settled && onAccept && (
          <Button variant="outline" onClick={onAccept} disabled={busy}>
            accept
          </Button>
        )}
        {!settled && onReject && file.status !== "unsnapshotted" && (
          <RejectControl onReject={onReject} busy={busy} />
        )}
      </div>
    </div>
  );
}
