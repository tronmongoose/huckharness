// Skill chips above the composer: up to three skills whose name or description
// matches the draft. A click puts `/skill <name> ` in front of the draft; no
// SKILL.md reaches the model until the operator sends it.

import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import type { SkillSuggestion } from "@/lib/types";

export const SUGGEST_DEBOUNCE_MS = 300;
const MIN_CHARS = 12;

export function SkillSuggest({ draft, onPick }: {
  draft: string; onPick: (name: string) => void;
}) {
  const [rows, setRows] = useState<SkillSuggestion[]>([]);
  const q = draft.trim();
  const wanted = q.length >= MIN_CHARS && !q.startsWith("/");
  useEffect(() => {
    if (!wanted) return setRows([]);
    let live = true;
    const t = setTimeout(async () => {
      const res = await apiFetch<{ suggestions: SkillSuggestion[] }>(
        `/v1/skills/suggest?q=${encodeURIComponent(q)}`);
      if (live) setRows((res?.suggestions ?? []).slice(0, 3));
    }, SUGGEST_DEBOUNCE_MS);
    return () => { live = false; clearTimeout(t); };
  }, [q, wanted]);
  if (!wanted || rows.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 pb-2" aria-label="suggested skills">
      <span className="label">skills</span>
      {rows.map((s) => (
        <button key={s.name} type="button" title={s.description} onClick={() => onPick(s.name)}
          className="font-mono text-[0.68rem] border border-rule rounded px-2 py-0.5 text-muted hover:text-ink hover:border-accent/50">
          /skill {s.name}
        </button>
      ))}
    </div>
  );
}
