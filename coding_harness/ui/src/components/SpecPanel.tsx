// Spec, then run: draft a plan in a read-only turn, edit it here, then run it
// at a chosen autonomy level. The plan lives in one server-side file per
// session (GET/PUT .../plan); this panel never names a path.

import { useEffect, useState } from "react";

import { API_BASE, notifyUnauthorized } from "@/lib/api";
import { ACT_LEVELS, levelByKey } from "@/lib/autonomy";
import type { SpecStatus, TodoItem } from "@/lib/types";
import { TaskList } from "./TaskList";
import { Button, SectionLabel } from "./shared";

interface PlanDoc {
  exists: boolean;
  text: string;
}

// Unlike apiPost, keeps the server's error message: a 400 here says why.
async function send(method: string, path: string, body?: unknown) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method,
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) notifyUnauthorized();
    const data = (await res.json().catch(() => ({}))) as Record<string, unknown>;
    return { ok: res.ok, data, error: res.ok ? null : String(data.error ?? `HTTP ${res.status}`) };
  } catch {
    return { ok: false, data: {}, error: "server unreachable" };
  }
}

function useSavedPlan(sessionId: string, version: number) {
  const [saved, setSaved] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    void send("GET", `/v1/sessions/${sessionId}/plan`).then((r) => {
      if (live && r.ok) setSaved((r.data as unknown as PlanDoc).text ?? "");
    });
    return () => {
      live = false;
    };
  }, [sessionId, version]);
  return [saved, setSaved] as const;
}

function LevelPicker({ value, onChange, disabled }: {
  value: string; onChange: (v: string) => void; disabled: boolean;
}) {
  return (
    <div className="inline-flex border border-rule rounded p-0.5 gap-0.5" role="group"
      aria-label="run autonomy">
      {ACT_LEVELS.map((l) => (
        <button key={l.key} type="button" aria-pressed={value === l.key} disabled={disabled}
          onClick={() => onChange(l.key)} title={l.hint}
          className={`px-2 py-1 rounded font-mono text-[0.68rem] uppercase tracking-[0.14em] ${
            value === l.key ? "bg-paper text-accent" : "text-muted hover:text-ink"
          }`}>
          {l.key}
        </button>
      ))}
    </div>
  );
}

export function SpecPanel({ sessionId, turnInFlight, spec, todos, autonomy }: {
  sessionId: string;
  turnInFlight: boolean;
  spec: SpecStatus;
  todos: TodoItem[];
  autonomy?: string | null;
}) {
  const [goal, setGoal] = useState("");
  const [saved, setSaved] = useSavedPlan(sessionId, spec.version);
  const [text, setText] = useState("");
  const [level, setLevel] = useState(autonomy && autonomy !== "off" ? autonomy : "low");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => setText(saved ?? ""), [saved]);

  const busy = turnInFlight || pending;
  const dirty = saved !== null && text !== saved;

  const run = async (fn: () => Promise<string | null>) => {
    setPending(true);
    setError(await fn());
    setPending(false);
  };
  const save = async () => {
    const r = await send("PUT", `/v1/sessions/${sessionId}/plan`, { text });
    if (r.ok) setSaved(text);
    return r.error;
  };
  const draft = () => run(async () =>
    (await send("POST", `/v1/sessions/${sessionId}/plan`, { goal: goal.trim() })).error);
  const act = () => run(async () => {
    const problem = dirty ? await save() : null;
    if (problem) return problem;
    return (await send("POST", `/v1/sessions/${sessionId}/act`, { autonomy: level })).error;
  });

  const shown = error ?? spec.error;
  return (
    <div className="space-y-4 text-sm">
      <div>
        <SectionLabel>goal</SectionLabel>
        <textarea aria-label="goal" value={goal} onChange={(e) => setGoal(e.target.value)}
          rows={2} disabled={busy} className="input font-serif text-sm resize-none"
          placeholder="What should change? The plan turn only reads." />
        <div className="mt-2">
          <Button onClick={() => void draft()} disabled={busy || !goal.trim()}>draft plan</Button>
        </div>
      </div>
      <div>
        <div className="flex items-baseline justify-between">
          <SectionLabel>plan</SectionLabel>
          <span className="label" role="status">
            {saved === null || (!saved && !text) ? "no plan" : dirty ? "unsaved" : "saved"}
          </span>
        </div>
        <textarea aria-label="plan" value={text} onChange={(e) => setText(e.target.value)}
          rows={12} disabled={busy} className="input font-mono text-[0.7rem] leading-relaxed" />
        <div className="mt-2">
          <Button variant="outline" onClick={() => void run(save)} disabled={busy || !dirty}>
            save
          </Button>
        </div>
      </div>
      <div className="space-y-2">
        <SectionLabel>run at</SectionLabel>
        <LevelPicker value={level} onChange={setLevel} disabled={busy} />
        <p className="text-xs text-muted italic">{levelByKey(level).hint}</p>
        <Button onClick={() => void act()} disabled={busy || !text.trim()}>run plan</Button>
      </div>
      {shown && <p className="text-xs text-danger" role="alert">{shown}</p>}
      <TaskList items={todos} />
    </div>
  );
}
