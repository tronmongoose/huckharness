// What the running turn is doing right now. A turn can sit minutes on a test
// baseline or behind another model in Ollama; this line says which, and how long.

import { useEffect, useState } from "react";

import { usePoll } from "@/hooks/usePoll";
import type { OllamaStatus, Phase } from "@/lib/types";

function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

// The wait explained from Ollama's residency: loaded means the queue, not a load.
export function waitText(model: string, ollama: OllamaStatus | null): string {
  if (!ollama) return `waiting for ${model}`;
  if (!ollama.reachable) return `waiting for ${model} · Ollama not answering`;
  const others = ollama.resident.filter((r) => r.model !== model);
  const loaded = others.length < ollama.resident.length;
  const held = others.map((r) => `${r.model} ${r.size_gb} GB`).join(", ");
  if (loaded) return `waiting for ${model} · loaded, queued behind other requests`;
  return `waiting for ${model} · not loaded${held ? `, resident: ${held}` : ""}`;
}

export function phaseText(phase: Phase, now: number, ollama: OllamaStatus | null): string {
  switch (phase.kind) {
    case "starting":
      return "starting";
    case "baseline":
      return `running baseline: ${phase.detail}`;
    case "waiting":
      return waitText(phase.detail, ollama);
    case "generating": {
      const secs = (now - phase.since) / 1000;
      const rate = secs >= 1 ? ` · ${Math.round(phase.tokens / secs)} tok/s` : "";
      return `generating${phase.detail ? ` on ${phase.detail}` : ""}${rate}`;
    }
    case "tool":
      return `running ${phase.detail}`;
    case "approval":
      return `waiting for your approval: ${phase.detail}`;
    default:
      return "";
  }
}

export function StatusLine({ phase, autonomy }: { phase: Phase; autonomy?: string | null }) {
  const [now, setNow] = useState(Date.now());
  const active = phase.kind !== "idle";
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, [active]);
  // Residency matters only while waiting on a model; don't poll otherwise.
  const ollama = usePoll<OllamaStatus>(phase.kind === "waiting" ? "/v1/ollama" : null, 3000);
  const level = autonomy ? (
    <span className="shrink-0 text-muted" title="autonomy level">autonomy {autonomy}</span>
  ) : null;
  if (!active) {
    return level ? <p className="font-mono text-[0.7rem] flex py-1.5">{level}</p> : null;
  }
  const urgent = phase.kind === "approval";
  const tone = urgent ? "text-accent" : "text-muted";
  // The thread's thinking block says what the turn is on; this line keeps
  // the clock, and repeats only an approval wait, which needs the operator.
  return (
    <p className={`font-mono text-[0.7rem] ${tone} flex items-center gap-2 py-1.5`}>
      {urgent && <span className="truncate" role="status">{phaseText(phase, now, ollama)}</span>}
      <span className="ml-auto shrink-0">{clock(now - phase.since)}</span>
      {level}
    </p>
  );
}
