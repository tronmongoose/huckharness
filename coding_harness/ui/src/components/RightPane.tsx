// Right pane: Access / Permissions / Activity / Spec / Changes / Git tabs. Quiet text-control tabs,
// hairline separation — no button groups, no boxes beyond the JIT card.

import { useEffect, useState } from "react";

import { apiPost } from "@/lib/api";
import type {
  AuditRow,
  EnvelopeDict,
  GitCommitInfo,
  MemoryProposal,
  PendingPermission,
  PinnedNote,
  RecallInfo,
  SpecStatus,
  TodoItem,
  TurnBlock,
} from "@/lib/types";
import { ChangesPanel, type ChangesFocus } from "./ChangesPanel";
import { GitPanel } from "./GitPanel";
import {
  Button,
  EmptyState,
  KV,
  SectionLabel,
  SpecTable,
  TextAction,
} from "./shared";
import { PermissionCard } from "./PermissionCard";
import { SpecPanel } from "./SpecPanel";
import { MemoryReview } from "./MemoryReview";
import { PinnedNotes } from "./PinnedNotes";

type Tab = "access" | "permissions" | "activity" | "spec" | "changes" | "memory" | "git";

function AccessTab({
  sessionId,
  envelope,
  revoked,
}: {
  sessionId: string;
  envelope: EnvelopeDict | null;
  revoked: boolean;
}) {
  const [confirming, setConfirming] = useState(false);
  if (!envelope) {
    return (
      <EmptyState>
        No envelope. This session runs ungoverned, Sentinel only.
      </EmptyState>
    );
  }
  return (
    <div>
      {revoked && <p className="label text-danger mb-3">revoked</p>}
      {envelope.grants.length === 0 ? (
        <EmptyState>No live grants. Every tool call will ask.</EmptyState>
      ) : (
        <div className="space-y-4">
          {envelope.grants.map((g, i) => (
            <SpecTable key={i}>
              <KV k="tool" v={`${g.tool} (${g.access})`} />
              {g.path_glob && <KV k="scope" v={g.path_glob} />}
              <KV
                k="granted by"
                v={
                  g.expires_at
                    ? `${g.granted_by} · expires ${new Date(g.expires_at).toLocaleTimeString()}`
                    : g.granted_by
                }
              />
            </SpecTable>
          ))}
        </div>
      )}
      {!revoked && (
        <div className="mt-6">
          {confirming ? (
            <span className="flex items-center gap-4">
              <span className="text-xs italic text-muted">
                kills the envelope and interrupts the turn.
              </span>
              <Button
                variant="danger"
                onClick={() => {
                  void apiPost(`/v1/sessions/${sessionId}/revoke`, {
                    reason: "operator_ui",
                  });
                  setConfirming(false);
                }}
              >
                confirm revoke
              </Button>
              <TextAction onClick={() => setConfirming(false)}>
                keep
              </TextAction>
            </span>
          ) : (
            <Button variant="danger" onClick={() => setConfirming(true)}>
              revoke session
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

function PermissionsTab({
  sessionId,
  pending,
}: {
  sessionId: string;
  pending: PendingPermission[];
}) {
  if (pending.length === 0) {
    return (
      <EmptyState>
        Nothing pending. When the agent reaches outside its envelope, the
        request appears here and its turn waits for your decision.
      </EmptyState>
    );
  }
  return (
    <div>
      {pending.map((req) => (
        <PermissionCard key={req.req_id} sessionId={sessionId} request={req} />
      ))}
    </div>
  );
}

function ActivityTab({ trail }: { trail: AuditRow[] }) {
  if (trail.length === 0) {
    return <EmptyState>No audit entries this session yet.</EmptyState>;
  }
  return (
    <div className="space-y-2">
      {trail
        .slice()
        .reverse()
        .map((row, i) => (
          <div
            key={i}
            className="flex items-baseline gap-3 font-mono text-[0.68rem]"
          >
            <span
              className={
                row.allowed === false ? "text-accent" : "text-muted"
              }
            >
              {row.allowed === false ? "denied" : "ok"}
            </span>
            <span className="text-ink/90 w-14 shrink-0">
              {row.tool ?? row.kind ?? "—"}
            </span>
            <span className="text-muted truncate flex-1">
              {row.sentinel_reason ?? row.action ?? ""}
            </span>
            {row.hash && (
              <span className="text-muted" title={row.hash}>
                #{String(row.hash).slice(0, 8)}
              </span>
            )}
          </div>
        ))}
    </div>
  );
}

export function RightPane({
  sessionId,
  envelope,
  revoked,
  pending,
  trail,
  turnInFlight = false,
  spec = { version: 0, bytes: null, error: null },
  todos = [],
  autonomy = null,
  turns = [],
  changesFocus,
  reviewWrites,
  reviewSignal = null,
  proposals = [],
  pinned = [],
  recall = null,
  lastCommit = null,
}: {
  sessionId: string;
  envelope: EnvelopeDict | null;
  revoked: boolean;
  pending: PendingPermission[];
  trail: AuditRow[];
  turnInFlight?: boolean;
  spec?: SpecStatus;
  todos?: TodoItem[];
  autonomy?: string | null;
  turns?: TurnBlock[];
  changesFocus?: ChangesFocus;
  reviewWrites?: boolean;
  reviewSignal?: boolean | null;
  proposals?: MemoryProposal[];
  pinned?: PinnedNote[];
  recall?: RecallInfo | null;
  lastCommit?: GitCommitInfo | null;
}) {
  const [tab, setTab] = useState<Tab>(changesFocus ? "changes" : "access");
  const tabs: Tab[] = ["access", "permissions", "activity", "spec", "changes", "memory", "git"];
  useEffect(() => {
    if (changesFocus) setTab("changes");
  }, [changesFocus]);
  return (
    <aside className={`w-full ${tab === "changes" ? "md:w-[34rem]" : "md:w-80"} shrink-0 card p-4 self-start`}>
      <div
        className="flex flex-wrap border border-rule rounded p-0.5 gap-0.5 mb-4"
        role="tablist"
      >
        {tabs.map((t) => (
          <button
            key={t}
            type="button"
            role="tab"
            aria-selected={tab === t}
            onClick={() => setTab(t)}
            className={`flex-auto whitespace-nowrap flex items-center justify-center gap-1.5 px-2 py-1 rounded font-mono text-[0.68rem] uppercase tracking-[0.12em] ${
              tab === t
                ? "bg-paper text-ink"
                : "text-muted hover:text-ink"
            }`}
          >
            {t}
            {t === "memory" && proposals.length > 0 && (
              <span aria-label={`${proposals.length} proposals`}
                className="inline-flex items-center justify-center min-w-4 h-4 px-1 rounded-full bg-accent text-paper text-[0.6rem]">
                {proposals.length}
              </span>
            )}
            {t === "permissions" && pending.length > 0 && (
              <span className="inline-flex items-center justify-center min-w-4 h-4 px-1 rounded-full bg-accent text-paper text-[0.6rem]">
                {pending.length}
              </span>
            )}
          </button>
        ))}
      </div>
      <SectionLabel>
        {tab === "access"
          ? "what this session may touch"
          : tab === "permissions"
            ? "waiting on you"
            : tab === "spec"
              ? "plan, then run"
              : tab === "changes"
                ? "what the turns did to disk"
                : tab === "memory"
                  ? "what this session would remember"
                  : tab === "git"
                    ? "branch, commit, worktrees"
                    : "on the audit chain"}
      </SectionLabel>
      {tab === "access" && (
        <AccessTab sessionId={sessionId} envelope={envelope} revoked={revoked} />
      )}
      {tab === "permissions" && (
        <PermissionsTab sessionId={sessionId} pending={pending} />
      )}
      {tab === "activity" && <ActivityTab trail={trail} />}
      {tab === "spec" && (
        <SpecPanel sessionId={sessionId} turnInFlight={turnInFlight} spec={spec}
          todos={todos} autonomy={autonomy} />
      )}
      {tab === "changes" && (
        <ChangesPanel sessionId={sessionId} turns={turns}
          turnInFlight={turnInFlight} focus={changesFocus}
          reviewWrites={reviewWrites} reviewSignal={reviewSignal} />
      )}
      {tab === "memory" && (
        <>
          <MemoryReview sessionId={sessionId} proposals={proposals} />
          <PinnedNotes sessionId={sessionId} pinned={pinned} recall={recall} />
        </>
      )}
      {tab === "git" && (
        <GitPanel sessionId={sessionId} turnInFlight={turnInFlight}
          pending={pending} lastCommit={lastCommit} />
      )}
    </aside>
  );
}
