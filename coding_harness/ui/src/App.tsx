// App shell: left rail (sessions) · center thread · right governance pane.
// State flows from one SSE stream per active session plus two light polls.

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { ErrorBoundary } from "./components/ErrorBoundary";
import { Etching } from "./components/Etching";
import { Header, NotifyToggle, ShortcutHelp, ThemeToggle } from "./components/Header";
import { Mark } from "./components/Mark";
import type { ChangesFocus } from "./components/ChangesPanel";
import { RightPane } from "./components/RightPane";
import { BoardView } from "./components/BoardView";
import { BrainView } from "./components/BrainView";
import { FleetView } from "./components/FleetView";
import { Login } from "./components/Login";
import { SessionRail } from "./components/SessionRail";
import { Thread } from "./components/Thread";
import { SettingsView } from "./components/SettingsView";
import { SkillsView } from "./components/SkillsView";
import { useBoard } from "./hooks/useBoard";
import { useEventStream } from "./hooks/useEventStream";
import { useNotifications } from "./hooks/useNotifications";
import { usePoll } from "./hooks/usePoll";
import { useShortcuts } from "./hooks/useShortcuts";
import { apiPost } from "./lib/api";
import { useAuth } from "./lib/auth";
import { createSession } from "./lib/sessions";
import type { Attachment, EnvelopeDict, Healthz, SessionSummary } from "./lib/types";

interface SessionsResponse {
  sessions: SessionSummary[];
}

interface EnvelopeResponse {
  identity: string | null;
  envelope: EnvelopeDict | null;
}

type View = "sessions" | "board" | "skills" | "brain" | "fleet" | "settings";
const VIEWS: View[] = ["sessions", "board", "skills", "brain", "fleet", "settings"];

// Document-numbering convention (TX-02 datasheet / federal-manual lineage):
// every view is a numbered document, stamped in the masthead and footer.
const DOC_IDS: Record<View, { id: string; title: string }> = {
  sessions: { id: "BH-01", title: "Governed Sessions" },
  skills: { id: "BH-02", title: "Skill Library" },
  brain: { id: "BH-03", title: "Second Brain" },
  fleet: { id: "BH-04", title: "Fleet Ledger" },
  board: { id: "BH-05", title: "Session Board" },
  settings: { id: "BH-06", title: "Settings" },
};

// Print registration mark: the five palette inks as a swatch stack
// (U.S. Graphics poster-margin convention). Named per EPA 1977 practice.
const REGISTRATION_INKS = [
  "bg-accent", // Sunset Orange
  "bg-gold", // Gold Leaf
  "bg-danger", // Ember Red
  "bg-ink", // Sand White
  "bg-muted", // Slate
];

function RegistrationBar() {
  return (
    <div
      className="fixed left-2 bottom-8 hidden md:flex flex-col opacity-50 pointer-events-none select-none"
      aria-hidden="true"
    >
      {REGISTRATION_INKS.map((c) => (
        <span key={c} className={`w-2 h-2.5 ${c}`} />
      ))}
    </div>
  );
}

// Under md the side panes are drawers over the thread, closed by default.
const WIDE = "(min-width: 768px)";
function wide(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia(WIDE).matches;
}

function Drawer({ side, open, onClose, children }: {
  side: "left" | "right"; open: boolean; onClose: () => void; children: ReactNode;
}) {
  if (!open) return null;
  const edge = side === "left" ? "left-0 border-r" : "right-0 border-l";
  return (
    <>
      <div className="fixed inset-0 z-30 bg-paper/70 md:hidden" onClick={onClose} aria-hidden="true" />
      <div className={`fixed inset-y-0 ${edge} z-40 w-[88vw] max-w-sm overflow-y-auto bg-paper border-rule p-4
        pt-[max(1rem,env(safe-area-inset-top))] pb-[max(1rem,env(safe-area-inset-bottom))] flex flex-col
        md:static md:z-auto md:w-auto md:max-w-none md:overflow-visible md:bg-transparent md:border-0 md:p-0 min-h-0`}>
        {children}
      </div>
    </>
  );
}

// `bjorn` opens /?new=1: start a session at the server's defaults, once
// (StrictMode runs effects twice in dev), then drop the flag from the URL.
function useLaunchSession(health: Healthz | null, onCreated: (id: string) => void) {
  const done = useRef(false);
  useEffect(() => {
    if (done.current || !health) return;
    if (new URLSearchParams(window.location.search).get("new") !== "1") return;
    done.current = true;
    window.history.replaceState(null, "", window.location.pathname + window.location.hash);
    void createSession({ autonomy: health.default_autonomy, presetKey: "project" }).then(
      (id) => id && onCreated(id),
    );
  }, [health, onCreated]);
}

// The open session lives in the URL hash so a reload lands back on it.
function useActiveSession(): [string | null, (id: string | null) => void] {
  const read = () => new URLSearchParams(window.location.hash.slice(1)).get("s");
  const [id, setId] = useState<string | null>(read);
  useEffect(() => {
    const onHash = () => setId(read());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const select = useCallback((next: string | null) => {
    setId(next);
    const hash = next ? `#s=${next}` : "";
    window.history.replaceState(null, "", window.location.pathname + window.location.search + hash);
  }, []);
  return [id, select];
}

function Workspace() {
  const [view, setView] = useState<View>("sessions");
  const [activeId, setActiveId] = useActiveSession();
  const [paneOpen, setPaneOpen] = useState(false);
  const [railOpen, setRailOpen] = useState(wide);
  // Crossing the breakpoint resets the rail: open when wide, a closed drawer when narrow.
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return;
    const mq = window.matchMedia(WIDE);
    const onChange = (e: MediaQueryListEvent) => setRailOpen(e.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  const [changesFocus, setChangesFocus] = useState<ChangesFocus | undefined>();
  const openChanges = (turn: number) => {
    setPaneOpen(true);
    setChangesFocus({ turn, nonce: Date.now() });
  };
  const [seed, setSeed] = useState<{ text: string; nonce: number } | undefined>();
  // Notes waiting to go out with the next turn, per session.
  const [attachments, setAttachments] = useState<Record<string, Attachment[]>>({});
  const useSkill = (name: string) => {
    setSeed({ text: `/skill ${name} `, nonce: Date.now() });
    setView("sessions");
  };
  const sessionsRes = usePoll<SessionsResponse>("/v1/sessions", 3000);
  const health = usePoll<Healthz>("/v1/healthz", 30000);
  const sessions = sessionsRes?.sessions ?? [];
  const active = sessions.find((s) => s.session_id === activeId) ?? null;
  useLaunchSession(health, setActiveId);

  // A hash from before a server restart names a session that no longer
  // exists. Check once, on the first list: later ids may simply be newer
  // than the last poll.
  const hashChecked = useRef(false);
  useEffect(() => {
    if (hashChecked.current || !sessionsRes) return;
    hashChecked.current = true;
    if (activeId && !sessionsRes.sessions.some((s) => s.session_id === activeId)) {
      setActiveId(null);
    }
  }, [sessionsRes, activeId, setActiveId]);

  const newSession = useCallback(() => {
    void createSession({
      autonomy: health?.default_autonomy ?? "low", presetKey: "project",
    }).then((id) => id && setActiveId(id));
  }, [health]);

  // Attach to the open session, starting one if none is open, then go there.
  const attach = async (a: Attachment) => {
    let id = activeId;
    if (!id) {
      id = await createSession({ autonomy: health?.default_autonomy ?? "low", presetKey: "project" });
      if (!id) return;
      setActiveId(id);
    }
    const target = id;
    setAttachments((all) => {
      const list = all[target] ?? [];
      return list.some((x) => x.path === a.path) ? all : { ...all, [target]: [...list, a] };
    });
    setView("sessions");
  };

  const { state, conn } = useEventStream(activeId);
  const board = useBoard();
  useNotifications(board, activeId);
  // Envelope changes on JIT grants and revoke — poll it while a session is open.
  const envelopeRes = usePoll<EnvelopeResponse>(
    activeId ? `/v1/sessions/${activeId}/envelope` : null,
    4000,
  );

  // The stream's autonomy_change is newest; the 3s session poll covers the rest.
  const autonomy = state.autonomy ?? active?.autonomy ?? null;
  const pickSession = (id: string) => {
    setActiveId(id);
    if (!wide()) setRailOpen(false);
  };
  useShortcuts({
    toggleRail: () => setRailOpen((v) => !v),
    session: (n) => {
      const s = sessions[n - 1];
      if (s) { setView("sessions"); setActiveId(s.session_id); }
    },
  });

  const revoked = state.revoked || (envelopeRes?.envelope?.revoked ?? false);

  return (
    <div className="h-screen h-[100dvh] flex flex-col overflow-x-hidden">
      <Etching view={view} />
      <RegistrationBar />
      {/* Unigrid masthead: identity reversed out of a full-width title band,
          flush left to the first grid module; the document ID rides the band. */}
      <header className="border-b border-rule bg-card/60 pt-[env(safe-area-inset-top)]">
        <nav className="max-w-[96rem] mx-auto px-4 md:px-6 py-3 flex items-center flex-wrap gap-x-4 gap-y-2">
          {view === "sessions" && (
            <button type="button" onClick={() => setRailOpen((v) => !v)} aria-expanded={railOpen}
              aria-label="sessions" title="sessions (⌘/)"
              className="label px-2 py-1 rounded border border-rule hover:text-ink md:hidden">
              rail
            </button>
          )}
          <span className="flex items-center gap-2.5 text-accent">
            <Mark size={18} />
            <span className="font-serif text-lg tracking-[0.04em] text-ink hidden sm:inline">
              Bjorn Harness
            </span>
          </span>
          <span className="label hidden sm:inline">
            {DOC_IDS[view].id} · {DOC_IDS[view].title}
          </span>
          <div
            className="flex flex-wrap border border-rule rounded p-0.5 gap-0.5 ml-auto order-last w-full sm:inline-flex sm:flex-nowrap sm:order-none sm:w-auto"
            role="group"
            aria-label="console view"
          >
            {VIEWS.map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => setView(v)}
                className={`flex-1 sm:flex-none px-2 sm:px-3 py-1 rounded font-mono text-[0.68rem] uppercase tracking-[0.14em] ${
                  view === v
                    ? "bg-card text-accent"
                    : "text-muted hover:text-ink"
                }`}
              >
                {v}
              </button>
            ))}
          </div>
          <span className="flex items-center gap-2 ml-auto sm:ml-0">
            <NotifyToggle />
            <ThemeToggle />
            <ShortcutHelp />
          </span>
        </nav>
      </header>
      <div className="max-w-[96rem] mx-auto px-4 md:px-6 py-3 md:py-5 w-full flex-1 flex flex-col min-h-0">
      <main className="flex gap-4 md:gap-8 flex-1 min-h-0 min-w-0">
        {view === "skills" ? (
          <ErrorBoundary label="skills view">
            <SkillsView onUse={useSkill} />
          </ErrorBoundary>
        ) : view === "brain" ? (
          <ErrorBoundary label="brain view">
            <BrainView onAttach={(a) => void attach(a)}
              onPin={activeId ? (a) => void apiPost(`/v1/sessions/${activeId}/pin`,
                { path: a.path, pinned: true }) : undefined} />
          </ErrorBoundary>
        ) : view === "board" ? (
          <ErrorBoundary label="board view">
            <BoardView board={board} onSelect={(id) => { setActiveId(id); setView("sessions"); }} />
          </ErrorBoundary>
        ) : view === "fleet" ? (
          <ErrorBoundary label="fleet view">
            <FleetView />
          </ErrorBoundary>
        ) : view === "settings" ? (
          <ErrorBoundary label="settings view">
            <SettingsView />
          </ErrorBoundary>
        ) : (
          <>
        <Drawer side="left" open={railOpen} onClose={() => setRailOpen(false)}>
        <ErrorBoundary label="session rail">
          <SessionRail
            sessions={sessions}
            activeId={activeId}
            onSelect={pickSession}
            onCreated={pickSession}
            defaultAutonomy={health?.default_autonomy}
            onPickSkill={useSkill}
          />
        </ErrorBoundary>
        </Drawer>

        {activeId ? (
          <>
            <div className="min-w-0 flex-1 flex flex-col min-h-0 gap-3">
              {active && (
                <Header
                  session={active}
                  cwd={health?.cwd ?? null}
                  conn={conn}
                  turnInFlight={state.turnInFlight}
                  paneOpen={paneOpen}
                  onTogglePane={() => setPaneOpen((v) => !v)}
                  pendingCount={state.pending.length}
                  context={state.context}
                />
              )}
              <ErrorBoundary label="thread">
                <Thread
                  sessionId={activeId}
                  thread={state.thread}
                  pending={state.pending}
                  phase={state.phase}
                  turnInFlight={state.turnInFlight}
                  disabled={revoked}
                  onNewSession={newSession}
                  seed={seed}
                  attachments={attachments[activeId] ?? []}
                  onAttachmentsChange={(list: Attachment[]) =>
                    setAttachments((all) => ({ ...all, [activeId]: list }))}
                  todos={state.todos}
                  autonomy={autonomy}
                  onOpenChanges={openChanges}
                  resumed={state.resumed}
                />
              </ErrorBoundary>
            </div>
            <Drawer side="right" open={paneOpen} onClose={() => setPaneOpen(false)}>
            <ErrorBoundary label="governance pane">
              <RightPane
                sessionId={activeId}
                envelope={envelopeRes?.envelope ?? null}
                revoked={revoked}
                pending={state.pending}
                trail={state.auditTrail}
                turnInFlight={state.turnInFlight}
                spec={state.spec}
                todos={state.todos}
                autonomy={autonomy}
                turns={state.thread}
                changesFocus={changesFocus}
                reviewWrites={active?.review_writes}
                reviewSignal={state.reviewWrites}
                proposals={state.proposals}
                pinned={state.pinned}
                recall={state.recall}
                lastCommit={state.lastCommit}
              />
            </ErrorBoundary>
            </Drawer>
          </>
        ) : (
          <section className="flex-1 flex items-center relative">
            <div
              className="absolute right-8 top-1/2 -translate-y-1/2 text-gold opacity-[0.12] pointer-events-none select-none"
              aria-hidden="true"
            >
              <Mark size={360} annotated />
            </div>
            <div className="max-w-md relative">
              <h1 className="font-serif text-3xl mb-4">Bjorn Harness</h1>
              <p className="text-base leading-relaxed text-muted">
                Governed sessions over a local coding agent. Create a session
                with a scoped envelope. The agent acts freely inside it and
                asks you, just in time, for anything outside. Every action
                lands on a tamper-evident audit chain under the identity you
                chose.
              </p>
            </div>
          </section>
        )}
          </>
        )}
      </main>

      {view === "fleet" && (
      <footer className="mt-10 pt-4 border-t border-rule">
        <div className="flex justify-between">
          <span className="label">
            {DOC_IDS[view].id} · rev {new Date().toISOString().slice(0, 10)} ·
            Governed by Carryall
          </span>
          <span className="label">
            every action identity-stamped on a hash-chained audit log
          </span>
        </div>
        <p className="font-mono text-[0.6rem] text-muted/70 mt-2">
          inks: Dusk Navy · Sand White · Sunset Orange · Gold Leaf · Ember Red
          — engravings: Ramelli, Le diverse et artificiose machine (1588) ·
          Cole's orrery · Babbage Difference Engine (1853), public domain via
          Wikimedia Commons
        </p>
      </footer>
      )}
      </div>
    </div>
  );
}

// Off loopback the server wants a login first; on loopback status says so and
// the workspace mounts straight away.
export default function App() {
  const auth = useAuth();
  if (auth.state === "checking") return null;
  if (auth.state === "login") return <Login onSuccess={auth.recheck} tokenHint={auth.tokenHint} />;
  return <Workspace />;
}
