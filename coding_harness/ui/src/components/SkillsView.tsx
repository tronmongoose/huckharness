// Skills tab: every skill the model is told about, read in full, used in a
// session, or written. Only skills under ~/.config/bjorn/skills are editable;
// borrowed roots (~/.claude/skills) can be copied under a new name.

import { useEffect, useMemo, useState } from "react";

import { usePoll } from "@/hooks/usePoll";
import { apiFetch, apiPost } from "@/lib/api";
import type { SkillDetail, SkillInfo } from "@/lib/types";
import { Markdown } from "./Markdown";
import { Button, EmptyState, SectionLabel, TextAction } from "./shared";

interface Draft {
  name: string;
  description: string;
  body: string;
}

// Frontmatter off, so the editor holds only what the person wrote.
export function skillBody(text: string): string {
  return text.replace(/^---\n[\s\S]*?\n---\n+/, "").trim();
}

function Editor({ draft, onSaved, onCancel }: {
  draft: Draft; onSaved: (name: string) => void; onCancel: () => void;
}) {
  const [d, setD] = useState(draft);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setError(null);
    const res = await apiPost(`/v1/skills/${encodeURIComponent(d.name)}`,
      { description: d.description, body: d.body });
    if (res) onSaved(d.name);
    else setError("not saved: use lowercase-dashed names, a one-line description, and a new name for borrowed skills");
  };
  return (
    <div className="space-y-3">
      <input aria-label="skill name" value={d.name} placeholder="skill-name"
        onChange={(e) => setD({ ...d, name: e.target.value })} className="input" />
      <input aria-label="skill description" value={d.description}
        placeholder="one line: when to use it"
        onChange={(e) => setD({ ...d, description: e.target.value })} className="input" />
      <textarea aria-label="skill body" value={d.body} rows={18}
        onChange={(e) => setD({ ...d, body: e.target.value })}
        className="input font-mono text-xs leading-relaxed" />
      {error && <p className="text-xs text-danger">{error}</p>}
      <div className="flex gap-3">
        <Button onClick={() => void save()} disabled={!d.name || !d.description}>save</Button>
        <TextAction onClick={onCancel}>cancel</TextAction>
      </div>
    </div>
  );
}

export function SkillsView({ onUse }: { onUse: (name: string) => void }) {
  const [refresh, setRefresh] = useState(0);
  const list = usePoll<{ skills: SkillInfo[] }>(`/v1/skills?r=${refresh}`, 60000);
  const [filter, setFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  const [editing, setEditing] = useState<Draft | null>(null);

  const skills = useMemo(() => (list?.skills ?? []).filter((s) =>
    `${s.name} ${s.description}`.toLowerCase().includes(filter.toLowerCase())), [list, filter]);

  useEffect(() => {
    if (!selected) return setDetail(null);
    let live = true;
    void apiFetch<SkillDetail>(`/v1/skills/${encodeURIComponent(selected)}`)
      .then((d) => live && setDetail(d));
    return () => { live = false; };
  }, [selected, refresh]);

  const saved = (name: string) => {
    setEditing(null);
    setSelected(name);
    setRefresh((r) => r + 1);
  };

  return (
    <div className="flex-1 min-h-0 flex gap-8">
      <aside className="w-72 shrink-0 flex flex-col min-h-0">
        <div className="flex gap-3 items-center mb-3">
          <Button onClick={() => setEditing({ name: "", description: "", body: "" })}>new skill</Button>
        </div>
        <input aria-label="filter skills" value={filter} placeholder="filter"
          onChange={(e) => setFilter(e.target.value)} className="input mb-3" />
        <SectionLabel>{skills.length} skills</SectionLabel>
        <ul className="space-y-0.5 overflow-y-auto min-h-0">
          {skills.map((s) => (
            <li key={s.name}>
              <button type="button" onClick={() => { setEditing(null); setSelected(s.name); }}
                className={`text-left w-full rounded px-2 py-1.5 ${
                  selected === s.name ? "bg-card text-ink" : "text-muted hover:text-ink hover:bg-card/60"}`}>
                <span className="flex items-baseline gap-2">
                  <span className="font-mono text-[0.75rem] text-ink truncate">{s.name}</span>
                  <span className="label ml-auto">{s.source}</span>
                </span>
                <span className="block text-xs truncate">{s.description}</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className="flex-1 min-w-0 overflow-y-auto pr-2 max-w-4xl">
        {editing ? (
          <Editor draft={editing} onSaved={saved} onCancel={() => setEditing(null)} />
        ) : detail ? (
          <article>
            <div className="flex items-center gap-4 mb-4 flex-wrap">
              <h2 className="font-serif text-2xl">{detail.name}</h2>
              <span className="label">{detail.source}</span>
              <span className="ml-auto flex gap-3 items-center">
                <Button onClick={() => onUse(detail.name)}>use in session</Button>
                {detail.editable ? (
                  <TextAction onClick={() => setEditing({ name: detail.name,
                    description: detail.description, body: skillBody(detail.text) })}>edit</TextAction>
                ) : (
                  <TextAction onClick={() => setEditing({ name: `${detail.name}-mine`,
                    description: detail.description, body: skillBody(detail.text) })}>
                    copy to bjorn
                  </TextAction>
                )}
              </span>
            </div>
            <Markdown text={skillBody(detail.text)} />
          </article>
        ) : (
          <EmptyState>
            Pick a skill to read it. A skill is a SKILL.md the model is told about; use one in a
            session to run a task under its instructions.
          </EmptyState>
        )}
      </section>
    </div>
  );
}
