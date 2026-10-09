// Skill buckets as a datasheet grid: one cell per use case with its count and
// the first few skill names, so forty skills read as a short menu.

import type { SkillBucket, SkillInfo } from "@/lib/types";

const PREVIEW = 3;

export function SkillTiles({ buckets, skills, onOpen }: {
  buckets: SkillBucket[]; skills: SkillInfo[]; onOpen: (bucket: string) => void;
}) {
  const shown = buckets.filter((b) => b.count > 0);
  return (
    // Each cell draws its own right and bottom rule, so a short last row
    // leaves page background, not a filled grey gap.
    <ul className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-3 border-l border-t border-rule"
      aria-label="skill buckets">
      {shown.map((b, i) => {
        const names = skills.filter((s) => s.bucket === b.name).slice(0, PREVIEW).map((s) => s.name);
        return (
          <li key={b.name} className="bg-paper border-r border-b border-rule">
            <button type="button" onClick={() => onOpen(b.name)} aria-label={`open ${b.name}`}
              className="text-left w-full h-full p-4 hover:bg-card focus-visible:outline-none
                focus-visible:ring-2 focus-visible:ring-accent/40">
              <span className="flex items-baseline justify-between gap-3 mb-3">
                <span className="label">SK-{String(i + 1).padStart(2, "0")}</span>
                <span className="font-mono text-2xl text-ink tabular-nums" data-testid="tile-count">
                  {b.count}
                </span>
              </span>
              <span className="block font-serif text-lg text-ink mb-2">{b.name}</span>
              <span className="block border-t border-rule/60 pt-2 font-mono text-[0.7rem] text-muted truncate">
                {names.join(" · ")}
                {b.count > names.length ? ` · +${b.count - names.length}` : ""}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
