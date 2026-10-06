// Session header: the project, the session's model (switchable between
// turns), mode, the connection heartbeat, and the governance-pane toggle.

import { useEffect, useLayoutEffect, useState } from "react";

import type { ConnState } from "@/hooks/useEventStream";
import { useNotifyPermission } from "@/hooks/useNotifications";
import { SHORTCUTS } from "@/hooks/useShortcuts";
import { applyTheme, nextTheme, readTheme, saveTheme, type ThemeChoice } from "@/lib/theme";
import { usePoll } from "@/hooks/usePoll";
import { setSessionKind, setSessionModel } from "@/lib/sessions";
import { apiPost } from "@/lib/api";
import type { ContextUse, GitStatus, ModelInfo, ModelsResponse, SessionSummary } from "@/lib/types";
import { ContextMeter } from "./ContextMeter";
import { ProjectPicker } from "./ProjectPicker";
import { TextAction } from "./shared";

// What a model can do, from the forced tool-call probe (modes/model_probe.py).
export function capability(m: ModelInfo): string {
  if (m.backend !== "ollama") return m.text_only ? "text only" : "";
  if (m.probing) return "probing…";
  if (m.agent === true) return "agent";
  if (m.agent === false) return "chat only";
  return "untested";
}

function ModelPicker({ session, turnInFlight }: { session: SessionSummary; turnInFlight: boolean }) {
  const [fast, setFast] = useState(false);
  const models = usePoll<ModelsResponse>("/v1/models", fast ? 4000 : 60000);
  useEffect(() => setFast(Boolean(models?.models.some((m) => m.probing))), [models]);
  const [error, setError] = useState<string | null>(null);
  // The session list polls every few seconds; hold the choice locally until
  // the server's summary catches up so the picker doesn't snap back.
  const [pending, setPending] = useState<string | null>(null);
  const server = session.explicit_model ? session.model : "auto";
  useEffect(() => setPending(null), [server, session.session_id]);
  const value = pending ?? server;
  const chosen = models?.models.find((m) => m.id === value);
  const change = async (tag: string) => {
    setError(null);
    setPending(tag);
    const picked = models?.models.find((m) => m.id === tag);
    if (picked && picked.backend === "ollama" && picked.agent == null && !picked.probing) {
      void apiPost("/v1/models/probe", { model: tag });
      setFast(true);
    }
    if (!(await setSessionModel(session.session_id, tag))) {
      setPending(null);
      setError("switch refused");
    }
  };
  return (
    <span className="flex items-center gap-2 min-w-0 max-w-full">
      <label className="label" htmlFor="session-model">model</label>
      <select
        id="session-model"
        value={value}
        disabled={turnInFlight}
        onChange={(e) => void change(e.target.value)}
        className="input bg-card w-auto min-w-0 max-w-full truncate py-1"
        title={turnInFlight ? "switch after this turn ends" : "applies to every later turn"}
      >
        <option value="auto">auto (router)</option>
        {value !== "auto" && !models?.models.some((m) => m.id === value) && (
          <option value={value}>{value}</option>
        )}
        {models?.models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.label ?? m.id}
            {capability(m) ? `  · ${capability(m)}` : ""}
            {m.loaded ? "  · loaded" : ""}
          </option>
        ))}
      </select>
      {error && <span className="text-xs text-danger">{error}</span>}
      {chosen?.agent === false && (
        <span className="text-xs text-accent italic" title={`${chosen.id} failed a forced tool call`}>
          chat only: won't use tools, pick an agent model to edit code
        </span>
      )}
    </span>
  );
}

// The checkout's branch beside the project picker; "detached" when HEAD is.
export function BranchTag() {
  const status = usePoll<GitStatus>("/v1/git", 5000);
  if (!status || status.error) return null;
  const detached = status.detached;
  const name = detached ? (status.head ?? "").slice(0, 8) : status.branch;
  if (!name) return null;
  return (
    <span className="font-mono text-xs text-muted" title={detached ? "detached HEAD" : "branch"}>
      <span aria-label="branch" className="text-ink">{name}</span>
      {detached && <span className="text-accent"> (detached)</span>}
    </span>
  );
}

// Code sends the tools and the code-role model; chat sends none and the
// chat-role model. Switching moves the model with it.
function KindToggle({ session, turnInFlight }: { session: SessionSummary; turnInFlight: boolean }) {
  const [pending, setPending] = useState<string | null>(null);
  const server = session.kind ?? "code";
  useEffect(() => setPending(null), [server, session.session_id]);
  const value = pending ?? server;
  const pick = async (kind: "code" | "chat") => {
    if (kind === value) return;
    setPending(kind);
    if (!(await setSessionKind(session.session_id, kind))) setPending(null);
  };
  return (
    <span className="inline-flex border border-rule rounded p-0.5 gap-0.5" role="group" aria-label="session kind"
      title={turnInFlight ? "switch after this turn ends" : "code uses tools; chat answers without them"}>
      {(["code", "chat"] as const).map((k) => (
        <button key={k} type="button" aria-pressed={value === k} disabled={turnInFlight}
          onClick={() => void pick(k)}
          className={`px-2 py-0.5 rounded font-mono text-[0.65rem] uppercase tracking-[0.14em] ${
            value === k ? "bg-card text-accent" : "text-muted hover:text-ink"}`}>
          {k}
        </button>
      ))}
    </span>
  );
}

export function Header({
  session,
  cwd,
  conn,
  turnInFlight,
  paneOpen,
  onTogglePane,
  pendingCount,
  context,
}: {
  session: SessionSummary;
  context: ContextUse | null;
  cwd: string | null;
  conn: ConnState;
  turnInFlight: boolean;
  paneOpen: boolean;
  onTogglePane: () => void;
  pendingCount: number;
}) {
  return (
    <header className="flex items-center flex-wrap gap-x-5 gap-y-2 pb-3 border-b border-rule">
      <ProjectPicker cwd={cwd} />
      <BranchTag />
      <KindToggle session={session} turnInFlight={turnInFlight} />
      <ModelPicker session={session} turnInFlight={turnInFlight} />
      <span className="label">{session.mode}</span>
      <ContextMeter context={context} />
      <span className="flex items-center gap-1.5 ml-auto">
        <span
          className={`inline-block w-1.5 h-1.5 rounded-full ${
            conn === "live" ? "bg-accent animate-heartbeat" : "bg-muted"
          }`}
        />
        <span className="label">{conn}</span>
      </span>
      <TextAction onClick={onTogglePane} tone={pendingCount > 0 ? "accent" : "default"}>
        {paneOpen ? "hide governance" : `governance${pendingCount ? ` (${pendingCount})` : ""}`}
      </TextAction>
    </header>
  );
}

// Theme toggle for the masthead: cycles system, light, dark.
export function ThemeToggle() {
  const [choice, setChoice] = useState<ThemeChoice>(readTheme);
  useLayoutEffect(() => applyTheme(choice), [choice]);
  return (
    <button type="button" title="theme: system, light or dark"
      onClick={() => { const n = nextTheme(choice); saveTheme(n); setChoice(n); }}
      className="label px-2 py-1 rounded border border-rule hover:text-ink">
      {choice}
    </button>
  );
}

// Opt-in for board notifications; the permission prompt needs a click.
export function NotifyToggle() {
  const [state, ask] = useNotifyPermission();
  if (state === "unsupported") return null;
  const word = state === "granted" ? "alerts on" : state === "denied" ? "alerts blocked" : "alerts off";
  return (
    <button type="button" onClick={ask} disabled={state !== "default"}
      title={state === "denied" ? "notifications are blocked in the browser's site settings"
        : "notify when a session finishes, fails or needs approval"}
      className="label px-2 py-1 rounded border border-rule hover:text-ink disabled:hover:text-muted">
      {word}
    </button>
  );
}

// "?" opens the keyboard-shortcut sheet.
export function ShortcutHelp() {
  const [open, setOpen] = useState(false);
  return (
    <span className="relative">
      <button type="button" aria-label="keyboard shortcuts" aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="label px-2 py-1 rounded border border-rule hover:text-ink">?</button>
      {open && (
        <div role="dialog" aria-label="keyboard shortcuts"
          className="card absolute right-0 top-full mt-2 z-50 p-3 w-64 shadow-lg">
          <p className="label mb-2">shortcuts · ⌘ or Ctrl</p>
          <dl className="grid grid-cols-[4.5rem_1fr] gap-y-1 text-xs">
            {SHORTCUTS.map((s) => (
              <div key={s.keys} className="contents">
                <dt className="font-mono text-ink">{s.keys}</dt>
                <dd className="text-muted">{s.does}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </span>
  );
}
