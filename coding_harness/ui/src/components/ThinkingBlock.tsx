// Under a running turn: a spinner, a rotating instrument verb, and one line
// saying what is actually happening. Tells the operator the ask landed and
// the agent is on it, without pretending to know more than the event stream.

import { useEffect, useState } from "react";

import { usePoll } from "@/hooks/usePoll";
import { phaseWord } from "@/lib/thinkingWords";
import type { OllamaStatus, Phase } from "@/lib/types";
import { phaseText } from "./StatusLine";

const GLYPHS = ["◐", "◓", "◑", "◒"];
const SPIN_MS = 125;
const WORD_MS = 2400;
const CALM_WORD_MS = 5000;

function reducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function clock(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

export function ThinkingBlock({ phase, turn, summary }: {
  phase: Phase; turn: number; summary?: string;
}) {
  const calm = reducedMotion();
  const [frame, setFrame] = useState(0);
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (calm) {
      const t = setInterval(() => setNow(Date.now()), 1000);
      return () => clearInterval(t);
    }
    const t = setInterval(() => { setFrame((f) => f + 1); setNow(Date.now()); }, SPIN_MS);
    return () => clearInterval(t);
  }, [calm]);
  const ollama = usePoll<OllamaStatus>(phase.kind === "waiting" ? "/v1/ollama" : null, 3000);
  const loading = phase.kind === "waiting" && Boolean(ollama)
    && !ollama?.resident.some((r) => r.model === phase.detail);
  const tick = Math.floor((now - phase.since) / (calm ? CALM_WORD_MS : WORD_MS));
  const word = phaseWord(phase, turn, tick, { summary, loading });
  const detail = phaseText(phase, now, ollama);
  const urgent = phase.kind === "approval";
  return (
    <div className="flex items-start gap-3 my-3" role="status" aria-live="polite">
      <span className={`font-mono text-base leading-none ${urgent ? "text-accent" : "text-gold"}`} aria-hidden="true">
        {calm ? "◉" : GLYPHS[frame % GLYPHS.length]}
      </span>
      <span className="min-w-0">
        <span key={calm ? undefined : word}
          className={`block font-serif text-base ${urgent ? "text-accent" : "thinking-word"} ${calm ? "" : "word-fade"}`}>
          {word}…
        </span>
        <span className="block font-mono text-[0.65rem] text-muted truncate">
          {detail}{detail ? " · " : ""}{clock(now - phase.since)}
        </span>
      </span>
    </div>
  );
}
