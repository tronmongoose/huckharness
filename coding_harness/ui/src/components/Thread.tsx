// Center pane: the thread (prose and tool calls in the order they happened),
// approval cards inline where the turn is waiting, and the composer.

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { apiPost } from "@/lib/api";
import { routeWords, showPrompt } from "@/lib/prompt";
import { setSessionModel } from "@/lib/sessions";
import type {
  Attachment, PendingPermission, Phase, ResumedInfo, TodoItem, TurnBlock, TurnResponse,
} from "@/lib/types";
import { AttachChips } from "./AttachChips";
import { Composer } from "./Composer";
import { Markdown } from "./Markdown";
import { PermissionCard } from "./PermissionCard";
import { StatusLine } from "./StatusLine";
import { TaskList } from "./TaskList";
import { SubagentCard } from "./SubagentCard";
import { ThinkingBlock } from "./ThinkingBlock";
import { StepView, stepSummary } from "./ToolStep";
import { Button, EmptyState } from "./shared";

// How close to the bottom (px) still counts as following the stream.
const FOLLOW_SLACK = 80;

// The prompt you just sent, shown before the server's turn_start arrives:
// "sending" while the POST is in flight, "received" once it is accepted.
function PendingTurn({ text, status, since }: { text: string; status: "sending" | "received"; since: number }) {
  return (
    <article className="py-5" data-testid="pending-turn">
      <div className="flex gap-3 mb-1">
        <span className="font-mono text-gold text-sm select-none" aria-hidden="true">›</span>
        <p className="text-base leading-relaxed whitespace-pre-wrap">{showPrompt(text).text}</p>
      </div>
      <p className="label ml-6">{status === "received" ? "received ✓" : "sending"}</p>
      <ThinkingBlock phase={{ kind: "starting", since, detail: "", tokens: 0 }} turn={0} />
    </article>
  );
}

function TurnView({ block, onOpenChanges, phase }: {
  block: TurnBlock;
  onOpenChanges?: (turn: number) => void;
  phase?: Phase; // only the open turn gets one
}) {
  const changed = block.filesChanged?.length ?? 0;
  const shown = showPrompt(block.prompt);
  const last = block.items[block.items.length - 1];
  const lastStep = last?.kind === "step" ? block.steps[last.index] : undefined;
  const thinking = !block.done && phase && phase.kind !== "idle" && last?.kind !== "text";
  return (
    <article className="py-5">
      <div className="flex gap-3 mb-3">
        <span className={`font-mono text-gold text-sm select-none ${
          !block.done && block.items.length === 0 ? "pulse-once" : ""}`} aria-hidden="true">›</span>
        <p className="text-base leading-relaxed whitespace-pre-wrap">
          {shown.skill && (
            <span className="font-mono text-[0.7rem] text-gold border border-gold/40 rounded px-1.5 py-0.5 mr-2">
              skill {shown.skill}
            </span>
          )}
          {shown.text}
        </p>
      </div>
      {shown.notes.length > 0 && (
        <div className="flex flex-wrap gap-2 mb-3 ml-6">
          {shown.notes.map((n) => (
            <span key={n} className="font-mono text-[0.65rem] text-muted border border-rule rounded px-1.5 py-0.5">
              note {n}
            </span>
          ))}
        </div>
      )}
      {block.items.map((item, i) =>
        item.kind === "text" ? (
          <Markdown key={i} text={item.text} />
        ) : item.kind === "steer" ? (
          <SteerNote key={i} text={item.text} applied={item.applied} dropped={item.dropped} />
        ) : (
          <div key={i}>
            <StepView step={block.steps[item.index]} />
            {block.steps[item.index].subagentId && block.subagents?.[block.steps[item.index].subagentId!] && (
              <SubagentCard agent={block.subagents[block.steps[item.index].subagentId!]} turnDone={block.done} />
            )}
          </div>
        ),
      )}
      {thinking && phase && (
        <ThinkingBlock phase={phase} turn={block.turn}
          summary={phase.kind === "tool" && lastStep ? stepSummary(lastStep) : undefined} />
      )}
      {block.errorText && (
        <p className="text-sm text-danger mt-2 font-mono">error: {block.errorText}</p>
      )}
      {(block.done || block.model) && (
        <p className="label mt-3">
          {[
            block.model,
            routeWords(block.routeReason),
            block.done ? block.haltedReason : null,
            block.done ? `${block.tokensOut ?? 0} tokens out` : null,
          ].filter(Boolean).join(" · ")}
        </p>
      )}
      {block.done && changed > 0 && (
        <button type="button" className="label mt-1 hover:text-ink underline decoration-rule"
          onClick={() => onOpenChanges?.(block.turn)}>
          {changed} {changed === 1 ? "file" : "files"} changed
        </button>
      )}
    </article>
  );
}

// An operator message sent mid-turn; "queued" until the next model call picks it up.
function SteerNote({ text, applied, dropped }: { text: string; applied: boolean; dropped?: boolean }) {
  return (
    <p className="my-2 ml-6 text-sm border-l-2 border-gold/60 pl-3 whitespace-pre-wrap">
      <span className="label mr-2">{applied ? "steered" : dropped ? "steer dropped, turn ended" : "steer queued"}</span>
      {text}
    </p>
  );
}

// Client-side slash commands. Anything else starting with "/" goes to the agent.
async function runSlash(
  line: string, sessionId: string, onNewSession: () => void,
): Promise<string | null> {
  const [cmd, arg] = line.trim().split(/\s+/, 2);
  if (cmd === "/model") {
    if (!arg) return "usage: /model <tag|auto>";
    return (await setSessionModel(sessionId, arg)) ? `model → ${arg}` : `/model: refused ${arg}`;
  }
  if (cmd === "/new" || cmd === "/clear") {
    onNewSession();
    return "";
  }
  if (cmd === "/compact") {
    const res = await apiPost<{ compacted: boolean; reason: string; messages_before: number; messages_after: number }>(
      `/v1/sessions/${sessionId}/compact`,
    );
    if (!res) return "/compact: refused (a turn may be running)";
    return res.compacted
      ? `compacted ${res.messages_before} → ${res.messages_after} messages`
      : `compaction skipped: ${res.reason}`;
  }
  if (cmd === "/stop") {
    await apiPost(`/v1/sessions/${sessionId}/interrupt`);
    return "stopping after the current step";
  }
  return null;
}

// Session ids start with their UTC start time (20260102T030405-ab12cd34).
function resumedDate(sessionId: string): string {
  const m = sessionId.match(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})/);
  if (!m) return "an earlier session";
  const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]));
  return d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

function ResumedBanner({ info, onDismiss }: { info: ResumedInfo; onDismiss: () => void }) {
  return (
    <div role="status" className="flex items-center gap-2 py-1.5 text-xs text-muted font-mono">
      <span className="flex-1">
        Resumed from {resumedDate(info.sessionId)}, {info.turns} {info.turns === 1 ? "turn" : "turns"}
      </span>
      <button type="button" aria-label="dismiss" className="hover:text-ink" onClick={onDismiss}>×</button>
    </div>
  );
}

export function Thread({
  sessionId,
  thread,
  pending,
  phase,
  turnInFlight,
  disabled,
  onNewSession,
  seed,
  attachments = [],
  onAttachmentsChange,
  todos = [],
  autonomy = null,
  onOpenChanges,
  resumed = null,
}: {
  sessionId: string;
  thread: TurnBlock[];
  pending: PendingPermission[];
  phase: Phase;
  turnInFlight: boolean;
  disabled: boolean;
  onNewSession: () => void;
  // Text to start the composer with; a new nonce re-applies the same text.
  seed?: { text: string; nonce: number };
  attachments?: Attachment[];
  onAttachmentsChange?: (list: Attachment[]) => void;
  todos?: TodoItem[];
  autonomy?: string | null;
  onOpenChanges?: (turn: number) => void;
  resumed?: ResumedInfo | null;
}) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  // The prompt just sent, until its turn_start makes the real block.
  const [sent, setSent] = useState<
    { text: string; status: "sending" | "received"; since: number; turns: number } | null>(null);
  useEffect(() => {
    if (sent && thread.length > sent.turns) setSent(null);
  }, [thread.length, sent]);
  useEffect(() => setSent(null), [sessionId]);
  const [notice, setNotice] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const following = useRef(true);
  const [dismissed, setDismissed] = useState<string | null>(null);

  // Follow the stream only while the reader is at the bottom, so scrolling up
  // to read an earlier turn is never yanked away by the next token.
  useLayoutEffect(() => {
    const el = scrollRef.current;
    if (el && following.current) el.scrollTop = el.scrollHeight;
  }, [thread, pending]);

  useEffect(() => {
    following.current = true;
    inputRef.current?.focus();
  }, [sessionId]);

  // A card waiting on the operator must be on screen: after a reload the
  // replayed history can push it below the fold.
  const firstPending = pending[0]?.req_id;
  useEffect(() => {
    if (!firstPending) return;
    const cards = scrollRef.current?.querySelectorAll<HTMLElement>("[data-req-id]") ?? [];
    const card = Array.from(cards).find((el) => el.dataset.reqId === firstPending);
    card?.scrollIntoView?.({ block: "start" });
  }, [firstPending, sessionId]);

  useEffect(() => {
    if (!seed) return;
    setDraft(seed.text);
    inputRef.current?.focus();
  }, [seed]);

  // Optimistic in-flight flag: blocks a double submit in the window before
  // the SSE turn_start arrives, dropped once the turn actually starts.
  useEffect(() => {
    if (turnInFlight) setSending(false);
  }, [turnInFlight]);

  const busy = turnInFlight || sending;

  const stop = () => void apiPost(`/v1/sessions/${sessionId}/interrupt`);

  const send = async () => {
    const message = draft.trim();
    if ((!message && attachments.length === 0) || disabled) return;
    if (message.startsWith("/")) {
      const out = await runSlash(message, sessionId, onNewSession);
      if (out !== null) {
        setDraft("");
        setNotice(out || null);
        return;
      }
    }
    if (turnInFlight) return void (message && steer(message));
    if (busy) return;
    // `/skill <name> <task>` runs the task under that skill's instructions.
    const skill = message.match(/^\/skill\s+(\S+)\s*([\s\S]*)$/);
    const body: Record<string, unknown> = skill ? { skill: skill[1], message: skill[2] } : { message };
    if (attachments.length > 0) body.attach = attachments.map((a) => a.path);
    setSending(true);
    setNotice(null);
    setDraft("");
    following.current = true;
    setSent({ text: message, status: "sending", since: Date.now(), turns: thread.length });
    const res = await apiPost<TurnResponse>(`/v1/sessions/${sessionId}/turn`, body);
    if (res !== null) onAttachmentsChange?.([]);
    if (res !== null) setSent((p) => (p ? { ...p, status: "received" } : p));
    if (res === null) {
      setSent(null);
      setDraft(message);
      setNotice("send failed. The server may be down or a turn is running. Prompt restored.");
      setSending(false);
    }
  };

  // Mid-turn text goes to the running turn's next model call, not a new turn.
  const steer = async (message: string) => {
    setDraft("");
    const res = await apiPost(`/v1/sessions/${sessionId}/steer`, { message });
    if (res === null) {
      setDraft(message);
      setNotice("steer failed. The turn may have just ended. Message restored.");
    }
  };

  return (
    <section className="min-w-0 flex-1 flex flex-col min-h-0">
      <div
        ref={scrollRef}
        onScroll={(e) => {
          const el = e.currentTarget;
          following.current = el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_SLACK;
        }}
        className="flex-1 overflow-y-auto overflow-x-hidden pr-2 min-h-0"
      >
        <div className="max-w-4xl divide-y divide-rule">
        {resumed && dismissed !== sessionId && (
          <ResumedBanner info={resumed} onDismiss={() => setDismissed(sessionId)} />
        )}
        {thread.length === 0 && !sent ? (
          <EmptyState>
            No turns yet. Ask for something. Tool calls inside the project run.
            Anything outside it asks you first.
          </EmptyState>
        ) : (
          thread.map((b, i) => (
            <TurnView key={b.turn} block={b} onOpenChanges={onOpenChanges}
              phase={i === thread.length - 1 ? phase : undefined} />
          ))
        )}
        {sent && (
          <PendingTurn text={sent.text} status={sent.status} since={sent.since} />
        )}
        {pending.map((req) => (
          <div key={req.req_id} data-req-id={req.req_id}>
            <PermissionCard sessionId={sessionId} request={req} />
          </div>
        ))}
        </div>
      </div>
      <div className="max-w-4xl w-full">
        <TaskList items={todos} />
        <StatusLine phase={phase} autonomy={autonomy} />
      </div>
      <div className="pt-3 border-t border-rule max-w-4xl w-full pb-[env(safe-area-inset-bottom)]">
        <AttachChips items={attachments}
          onRemove={(path) => onAttachmentsChange?.(attachments.filter((a) => a.path !== path))} />
        <div className="flex items-end gap-2">
          <Composer
            inputRef={inputRef}
            draft={draft}
            setDraft={setDraft}
            onSend={() => void send()}
            onStop={stop}
            busy={busy}
            disabled={disabled}
            placeholder={disabled ? "session revoked" : "Ask bjorn.  @ attaches a file, / lists commands, Enter sends, Esc stops."}
          />
          {turnInFlight && draft.trim() !== "" && (
            <Button variant="outline" onClick={() => void send()}
              title="adds this message before the running turn's next model call">
              steer
            </Button>
          )}
          {busy ? (
            <Button variant="danger" onClick={stop} title="stops after the current step">
              stop
            </Button>
          ) : (
            <Button onClick={() => void send()}
              disabled={disabled || (!draft.trim() && attachments.length === 0)}>
              send
            </Button>
          )}
        </div>
        <p className="text-xs text-muted italic mt-1.5 min-h-4 truncate">{notice}</p>
      </div>
    </section>
  );
}
