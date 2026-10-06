// The skills the model is told about, from the same index as its system
// prompt. Clicking one starts a `/skill <name> ` line in the composer.

import { usePoll } from "@/hooks/usePoll";
import type { SkillInfo } from "@/lib/types";
import { EmptyState, SectionLabel } from "./shared";

export function SkillsList({ onPick }: { onPick: (name: string) => void }) {
  const res = usePoll<{ skills: SkillInfo[] }>("/v1/skills", 60000);
  const skills = res?.skills ?? [];
  return (
    <div className="min-h-0 flex flex-col">
      <SectionLabel>Skills · {skills.length}</SectionLabel>
      {res && skills.length === 0 ? (
        <EmptyState>none found in ~/.config/bjorn/skills or ~/.claude/skills</EmptyState>
      ) : (
        <ul className="space-y-0.5 overflow-y-auto min-h-0">
          {skills.map((s) => (
            <li key={s.name}>
              <button
                type="button"
                onClick={() => onPick(s.name)}
                title={s.description}
                className="text-left w-full rounded px-2 py-1 text-muted hover:text-ink hover:bg-card/60"
              >
                <span className="block font-mono text-[0.72rem] text-ink truncate">{s.name}</span>
                <span className="block text-xs truncate">{s.description}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
