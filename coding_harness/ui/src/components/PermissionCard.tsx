// The JIT permission card — the page's hero interaction and its one boxed
// element (tenet 5: the box is earned). An out-of-envelope tool call blocks
// the agent's turn until the operator resolves it here. The broker denies
// on a 300s timeout, fail-closed.

import { useEffect, useState } from "react";

import { apiPost } from "@/lib/api";
import type { PendingPermission } from "@/lib/types";
import { DiffLines } from "./DiffView";
import { Button, KV } from "./shared";

const BROKER_TIMEOUT_S = 300;

function secondsLeft(createdAt: string): number {
  const created = new Date(createdAt).getTime();
  if (Number.isNaN(created)) return BROKER_TIMEOUT_S; // unparseable ⇒ don't show NaN
  const left = BROKER_TIMEOUT_S - (Date.now() - created) / 1000;
  return Math.max(0, Math.round(left));
}

export function PermissionCard({
  sessionId,
  request,
}: {
  sessionId: string;
  request: PendingPermission;
}) {
  const [expiry, setExpiry] = useState(30);
  const [countdown, setCountdown] = useState(() =>
    secondsLeft(request.created_at),
  );
  const [resolving, setResolving] = useState(false);

  useEffect(() => {
    const t = setInterval(
      () => setCountdown(secondsLeft(request.created_at)),
      1000,
    );
    return () => clearInterval(t);
  }, [request.created_at]);

  const resolve = async (
    decision: "allow_once" | "allow_always" | "deny",
    withExpiry = true,
  ) => {
    setResolving(true);
    const body: Record<string, unknown> = { decision };
    if (decision === "allow_always" && withExpiry) body.expiry_minutes = expiry;
    await apiPost(
      `/v1/sessions/${sessionId}/permissions/${request.req_id}`,
      body,
    );
    setResolving(false);
    // The permission_resolved SSE event (or the reconcile poll) clears the card.
  };

  if (request.kind === "review") {
    const { file_path: path, summary, diff } = request.args_preview;
    return (
      <div className="bg-paper border border-accent/70 rounded-lg p-4 my-3">
        <div className="flex items-baseline justify-between mb-2">
          <span className="label text-gold">review write</span>
          <span className="font-mono text-[0.65rem] text-muted">skips in {countdown}s</span>
        </div>
        <KV k="tool" v={request.tool} />
        <KV k="file" v={path ?? ""} />
        {summary && <KV k="summary" v={summary} />}
        <div className="mt-2">
          <DiffLines diff={diff ?? ""} />
        </div>
        <div className="flex items-center flex-wrap gap-3 mt-4">
          <Button onClick={() => resolve("allow_once")} disabled={resolving}>apply</Button>
          <Button variant="danger" onClick={() => resolve("deny")} disabled={resolving}>skip</Button>
          <Button variant="outline" onClick={() => resolve("allow_always", false)} disabled={resolving}
            title="apply this and every later write without asking">
            apply all
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-paper border border-accent/70 rounded-lg p-4 my-3">
      <div className="flex items-baseline justify-between mb-2">
        <span className="label text-gold">permission requested</span>
        <span className="font-mono text-[0.65rem] text-muted">
          denies in {countdown}s
        </span>
      </div>
      <KV k="tool" v={request.tool} />
      {Object.entries(request.args_preview).map(([k, v]) => (
        <KV key={k} k={k} v={v} />
      ))}
      <KV k="reason" v={request.reason} />
      <div className="flex items-center flex-wrap gap-3 mt-4">
        <Button onClick={() => resolve("allow_once")} disabled={resolving}>
          allow once
        </Button>
        <span className="flex items-center gap-1.5">
          <Button
            variant="outline"
            onClick={() => resolve("allow_always")}
            disabled={resolving}
          >
            allow always
          </Button>
          <input
            type="number"
            min={1}
            value={expiry}
            onChange={(e) => setExpiry(Number(e.target.value) || 30)}
            className="input w-14 text-center px-1"
            aria-label="grant expiry minutes"
          />
          <span className="label">min</span>
        </span>
        <Button
          variant="danger"
          onClick={() => resolve("deny")}
          disabled={resolving}
        >
          deny
        </Button>
      </div>
    </div>
  );
}
