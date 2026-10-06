// The right pane's changes tab: what a turn (or the whole session) did to the
// work tree, per file, with reject / accept, a whole-turn rewind, and the
// switch that parks every write for review before it lands.

import { useEffect, useState } from "react";

import { apiFetch, apiPost } from "@/lib/api";
import type { DiffResponse, TurnBlock } from "@/lib/types";
import { DiffView } from "./DiffView";
import { Button, EmptyState, TextAction } from "./shared";

type Pick = number | "all";

// Ask for a new view: a turn number, or null for the whole session.
export interface ChangesFocus {
  turn: number | null;
  nonce: number;
}

function latestFinished(turns: TurnBlock[]): Pick {
  const done = turns.filter((t) => t.done);
  return done.length > 0 ? done[done.length - 1].turn : "all";
}

// Paused while a turn runs: the server refuses the read then, and a refusal
// must never read as "no changes". The flip back to idle refetches.
function useDiff(sessionId: string, pick: Pick, refresh: unknown, paused: boolean) {
  const [diff, setDiff] = useState<DiffResponse | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    if (paused) return;
    let live = true;
    const q = pick === "all" ? "" : `?turn=${pick}`;
    void apiFetch<DiffResponse>(`/v1/sessions/${sessionId}/diff${q}`).then((res) => {
      if (!live) return;
      setFailed(res === null);
      if (res !== null) setDiff(res);
    });
    return () => {
      live = false;
    };
  }, [sessionId, pick, refresh, paused]);
  return { diff, failed };
}

function RewindControl({ label, disabled, onRewind }: {
  label: string; disabled: boolean; onRewind: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  if (!confirming) {
    return (
      <Button variant="danger" onClick={() => setConfirming(true)} disabled={disabled}
        title={disabled ? "wait for the turn to finish" : undefined}>
        {label}
      </Button>
    );
  }
  return (
    <span className="flex items-center gap-3">
      <span className="text-xs italic text-muted">undoes disk and history.</span>
      <Button variant="danger" disabled={disabled}
        onClick={() => { setConfirming(false); onRewind(); }}>
        confirm rewind
      </Button>
      <TextAction onClick={() => setConfirming(false)}>keep</TextAction>
    </span>
  );
}

export function ChangesPanel({
  sessionId,
  turns,
  turnInFlight,
  focus,
  reviewWrites,
  reviewSignal = null,
}: {
  sessionId: string;
  turns: TurnBlock[];
  turnInFlight: boolean;
  focus?: ChangesFocus;
  reviewWrites?: boolean; // from the session list poll
  reviewSignal?: boolean | null; // from the last resolved review request
}) {
  const [pick, setPick] = useState<Pick>(() => latestFinished(turns));
  const [nonce, setNonce] = useState(0);
  const [accepted, setAccepted] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [review, setReview] = useState(reviewWrites ?? false);
  const [notice, setNotice] = useState<string | null>(null);
  const reverted = turns.flatMap((t) => t.revertedFiles ?? []);
  const { diff, failed } = useDiff(
    sessionId, pick, `${nonce}:${reverted.length}`, turnInFlight);

  // The server owns review state: whichever source changed last wins.
  useEffect(() => {
    if (reviewWrites !== undefined) setReview(reviewWrites);
  }, [reviewWrites]);
  useEffect(() => {
    if (reviewSignal !== null) setReview(reviewSignal);
  }, [reviewSignal]);

  useEffect(() => {
    if (focus) setPick(focus.turn ?? "all");
  }, [focus]);
  useEffect(() => setAccepted(new Set()), [pick]);

  const done = turns.filter((t) => t.done);
  const latest = done.length > 0 ? done[done.length - 1].turn : 0;
  const act = async (path: string, body: unknown, failed: string) => {
    setBusy(true);
    const res = await apiPost(`/v1/sessions/${sessionId}/${path}`, body);
    setBusy(false);
    setNotice(res === null ? failed : null);
    setNonce((n) => n + 1);
    return res !== null;
  };
  const reject = (file: string) =>
    act("revert-file", { path: file, turn: pick === "all" ? null : pick }, `could not revert ${file}`);
  const rewindTurns = pick === "all" ? latest : latest - pick + 1;
  const rewind = () => act("rewind", { turns: rewindTurns }, "rewind refused");
  // A rewind drops turns from the thread; fall back when the pick went with them.
  useEffect(() => {
    if (pick !== "all" && !turns.some((t) => t.turn === pick)) setPick(latestFinished(turns));
  }, [turns, pick]);
  const toggleReview = async () => {
    const res = await apiPost(`/v1/sessions/${sessionId}/review`, { enabled: !review });
    if (res !== null) setReview(!review);
    else setNotice("review needs an interactive session");
  };

  return (
    <div>
      <div className="flex items-center gap-3 mb-3">
        <label className="label" htmlFor="changes-turn">turn</label>
        <select id="changes-turn" className="input w-auto" value={String(pick)}
          onChange={(e) => setPick(e.target.value === "all" ? "all" : Number(e.target.value))}>
          <option value="all">whole session</option>
          {done.map((t) => (
            <option key={t.turn} value={t.turn}>turn {t.turn}</option>
          ))}
        </select>
      </div>
      <label className="flex items-center gap-2 text-xs text-ink/90 mb-3">
        <input type="checkbox" checked={review} onChange={() => void toggleReview()} />
        Review writes before apply
      </label>
      {notice && <p className="text-xs italic text-danger mb-2">{notice}</p>}
      {failed ? (
        <p className="text-xs italic text-danger py-4">
          Could not load changes. The server refused or is down. They refresh when the turn ends.
        </p>
      ) : !diff ? (
        <EmptyState>{turnInFlight ? "Changes load when the turn ends." : "Loading changes."}</EmptyState>
      ) : diff.files.length === 0 ? (
        <EmptyState>No file changes {pick === "all" ? "this session" : `in turn ${pick}`}.</EmptyState>
      ) : (
        <div className="divide-y divide-rule">
          {diff.files.map((f) => (
            <DiffView key={f.path} file={f} busy={busy || turnInFlight}
              accepted={accepted.has(f.path)} reverted={reverted.includes(f.path)}
              onAccept={() => setAccepted((s) => new Set(s).add(f.path))}
              onReject={() => void reject(f.path)} />
          ))}
        </div>
      )}
      {latest > 0 && rewindTurns > 0 && (
        <div className="mt-4">
          <RewindControl
            label={pick === "all" ? "rewind session" : `rewind turn ${pick}${pick < latest ? " and later" : ""}`}
            disabled={turnInFlight || busy} onRewind={() => void rewind()} />
        </div>
      )}
    </div>
  );
}
