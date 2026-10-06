// A helper the turn sent out, nested under its Explore step: its task and
// model, a mini spinner while it works, and the tools it is calling. Once
// the turn ends it folds to one line.

import { useEffect, useState } from "react";

import type { Subagent } from "@/lib/types";

const GLYPHS = ["◐", "◓", "◑", "◒"];

export function SubagentCard({ agent, turnDone }: { agent: Subagent; turnDone: boolean }) {
  const [frame, setFrame] = useState(0);
  useEffect(() => {
    if (agent.done) return;
    const t = setInterval(() => setFrame((f) => f + 1), 150);
    return () => clearInterval(t);
  }, [agent.done]);
  const status = agent.done
    ? `done in ${agent.stepCount ?? agent.steps.length} steps${agent.halted && agent.halted !== "model_done" ? ` · ${agent.halted}` : ""}`
    : "working";
  return (
    <div className="ml-5 my-1.5 border-l-2 border-gold/40 pl-3" data-testid="subagent-card">
      <div className="flex items-baseline gap-2 font-mono text-[0.68rem]">
        <span className="text-gold" aria-hidden="true">{agent.done ? "✓" : GLYPHS[frame % GLYPHS.length]}</span>
        <span className="text-ink">scout</span>
        <span className="text-muted">{agent.model}</span>
        <span className="text-muted truncate min-w-0 flex-1">{agent.task}</span>
        <span className={agent.done ? "text-muted" : "text-accent"}>{status}</span>
      </div>
      {!turnDone && agent.steps.length > 0 && (
        <ul className="mt-1 space-y-0.5">
          {agent.steps.map((s, i) => (
            <li key={i} className="font-mono text-[0.65rem] text-muted flex gap-2">
              <span className="text-ink/80 w-10 shrink-0">{s.tool}</span>
              <span className="truncate">{s.summary}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
