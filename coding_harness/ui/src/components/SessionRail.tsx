// Left rail: one-click new session, the session list by title, and the full
// new-session form folded under "options". Quiet text controls (tenet 8).

import { useEffect, useState } from "react";

import { AUTONOMY_LEVELS, type AutonomyKey, levelByKey } from "@/lib/autonomy";
import { ENVELOPE_PRESETS, presetByKey } from "@/lib/presets";
import { createSession } from "@/lib/sessions";
import type { SessionSummary } from "@/lib/types";
import { EarlierList } from "./EarlierList";
import { SkillsList } from "./SkillsList";
import { Button, EmptyState, SectionLabel, TextAction } from "./shared";

function Options({
  identity, setIdentity, presetKey, setPresetKey, autonomy, setAutonomy,
}: {
  identity: string; setIdentity: (v: string) => void;
  presetKey: string; setPresetKey: (v: string) => void;
  autonomy: AutonomyKey; setAutonomy: (v: AutonomyKey) => void;
}) {
  return (
    <div className="space-y-3 text-sm mt-3">
      <div>
        <label className="label block mb-1" htmlFor="identity">acting as</label>
        <input id="identity" value={identity} onChange={(e) => setIdentity(e.target.value)}
          placeholder="optional" className="input" />
      </div>
      <div>
        <label className="label block mb-1" htmlFor="preset">envelope</label>
        <select id="preset" value={presetKey} onChange={(e) => setPresetKey(e.target.value)}
          className="input bg-card">
          {ENVELOPE_PRESETS.map((p) => (
            <option key={p.key} value={p.key}>{p.label}</option>
          ))}
        </select>
        <p className="text-xs text-muted mt-1 italic">{presetByKey(presetKey).description}</p>
      </div>
      <div>
        <span className="label block mb-1">autonomy</span>
        <div className="inline-flex border border-rule rounded p-0.5 gap-0.5" role="group"
          aria-label="session autonomy">
          {AUTONOMY_LEVELS.map((l) => (
            <button key={l.key} type="button" aria-pressed={autonomy === l.key}
              onClick={() => setAutonomy(l.key)}
              className={`px-2 py-1 rounded font-mono text-[0.68rem] uppercase tracking-[0.14em] ${
                autonomy === l.key ? "bg-paper text-accent" : "text-muted hover:text-ink"
              }`}>
              {l.key}
            </button>
          ))}
        </div>
        <p className="text-xs text-muted mt-1 italic">
          {levelByKey(autonomy).hint} · {levelByKey(autonomy).implies} mode
        </p>
      </div>
    </div>
  );
}

export function SessionRail({
  sessions,
  activeId,
  onSelect,
  onCreated,
  defaultAutonomy = "low",
  onOpenSkills,
}: {
  sessions: SessionSummary[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreated: (id: string) => void;
  defaultAutonomy?: string;
  onOpenSkills?: (bucket: string) => void;
}) {
  const [identity, setIdentity] = useState("");
  const [presetKey, setPresetKey] = useState("project");
  const [autonomy, setAutonomy] = useState<AutonomyKey>(levelByKey(defaultAutonomy).key);
  const [showOptions, setShowOptions] = useState(false);
  const [creating, setCreating] = useState(false);

  useEffect(() => setAutonomy(levelByKey(defaultAutonomy).key), [defaultAutonomy]);

  const create = async (kind: "code" | "chat" = "code") => {
    setCreating(true);
    const id = await createSession({ autonomy, presetKey, identity, kind });
    setCreating(false);
    if (id) onCreated(id);
  };

  return (
    <aside className="w-full md:w-60 shrink-0 flex flex-col min-h-0">
      <div className="flex items-center gap-3">
        <Button onClick={() => void create()} disabled={creating}>
          {creating ? "creating…" : showOptions ? "create session" : "new session"}
        </Button>
        <TextAction onClick={() => void create("chat")} disabled={creating}
          title="no tools, the chat-role model">new chat</TextAction>
        <TextAction onClick={() => setShowOptions((v) => !v)}>
          {showOptions ? "hide options" : "options"}
        </TextAction>
      </div>
      {showOptions && (
        <Options identity={identity} setIdentity={setIdentity} presetKey={presetKey}
          setPresetKey={setPresetKey} autonomy={autonomy} setAutonomy={setAutonomy} />
      )}
      {!showOptions && (
        <p className="text-xs text-muted italic mt-2">
          {autonomy} autonomy · {presetByKey(presetKey).label.toLowerCase()}
        </p>
      )}

      <hr className="rule my-5" />

      <SectionLabel>Sessions</SectionLabel>
      {sessions.length === 0 ? (
        <EmptyState>none yet</EmptyState>
      ) : (
        <ul className="space-y-1 overflow-y-auto min-h-0 max-h-[40vh]">
          {sessions.map((s) => (
            <li key={s.session_id}>
              <button type="button" onClick={() => onSelect(s.session_id)}
                className={`text-left w-full rounded px-2 py-1.5 border ${
                  s.session_id === activeId
                    ? "bg-card border-rule text-ink"
                    : "border-transparent text-muted hover:text-ink hover:bg-card/60"
                }`}>
                <span className="block text-sm truncate">
                  {s.title ?? "untitled session"}
                </span>
                <span className="block font-mono text-[0.6rem] text-muted truncate">
                  {s.turn_active ? "running · " : ""}
                  {s.explicit_model ? s.model : "auto"} · {s.mode}
                  {s.revoked ? " · revoked" : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <div className="mt-5">
        <EarlierList onOpened={onSelect} />
      </div>
      {onOpenSkills && (
        <>
          <hr className="rule my-5" />
          <SkillsList onOpen={onOpenSkills} />
        </>
      )}
    </aside>
  );
}
