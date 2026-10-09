// Skills tab in three levels: use-case tiles, one bucket's list, one skill.
// A search above the tiles reaches every bucket at once. Only skills under
// ~/.config/bjorn/skills are editable; borrowed roots can be copied under a
// new name.

import { useEffect, useMemo, useState } from "react";

import { usePoll } from "@/hooks/usePoll";
import { apiFetch } from "@/lib/api";
import type { SkillDetail, SkillInfo, SkillsResponse } from "@/lib/types";
import { Markdown } from "./Markdown";
import { type Draft, SkillEditor, skillBody } from "./SkillEditor";
import { SkillTiles } from "./SkillTiles";
import { Button, EmptyState, SectionLabel, TextAction } from "./shared";

export { skillBody };

const OTHER = "Other";

function SkillRows({ skills, showBucket, onPick }: {
  skills: SkillInfo[]; showBucket: boolean; onPick: (name: string) => void;
}) {
  return (
    <ul className="border-y border-rule divide-y divide-rule/60 max-w-4xl">
      {skills.map((s) => (
        <li key={s.name}>
          <button type="button" onClick={() => onPick(s.name)}
            className="text-left w-full px-2 py-2 text-muted hover:text-ink hover:bg-card/60">
            <span className="flex items-baseline gap-3">
              <span className="font-mono text-[0.78rem] text-ink truncate">{s.name}</span>
              {showBucket && <span className="label">{s.bucket ?? OTHER}</span>}
              <span className="label ml-auto">{s.source}</span>
            </span>
            <span className="block text-xs truncate">{s.description}</span>
          </button>
        </li>
      ))}
    </ul>
  );
}

function Crumbs({ bucket, skill, onHome, onBucket }: {
  bucket: string | null; skill: string | null; onHome: () => void; onBucket: () => void;
}) {
  return (
    <nav aria-label="skills breadcrumb" className="flex items-baseline gap-2 font-mono text-xs">
      {bucket || skill ? <TextAction onClick={onHome}>skills</TextAction> : <span className="label">skills</span>}
      {bucket && <span className="text-muted">/</span>}
      {bucket && (skill ? <TextAction onClick={onBucket}>{bucket}</TextAction>
        : <span className="text-ink">{bucket}</span>)}
      {skill && <span className="text-muted">/</span>}
      {skill && <span className="text-ink">{skill}</span>}
    </nav>
  );
}

function Detail({ detail, onUse, onEdit }: {
  detail: SkillDetail; onUse: (name: string) => void; onEdit: (d: Draft) => void;
}) {
  const draft = (name: string): Draft => ({ name, description: detail.description,
    body: skillBody(detail.text), category: detail.category ?? "" });
  return (
    <article className="max-w-4xl">
      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <h2 className="font-serif text-2xl">{detail.name}</h2>
        <span className="label">{detail.source}</span>
        <span className="ml-auto flex gap-3 items-center">
          <Button onClick={() => onUse(detail.name)}>use in session</Button>
          {detail.editable ? (
            <TextAction onClick={() => onEdit(draft(detail.name))}>edit</TextAction>
          ) : (
            <TextAction onClick={() => onEdit(draft(`${detail.name}-mine`))}>copy to bjorn</TextAction>
          )}
        </span>
      </div>
      <Markdown text={skillBody(detail.text)} />
    </article>
  );
}

function useDetail(selected: string | null, refresh: number): SkillDetail | null {
  const [detail, setDetail] = useState<SkillDetail | null>(null);
  useEffect(() => {
    setDetail(null);
    if (!selected) return;
    let live = true;
    void apiFetch<SkillDetail>(`/v1/skills/${encodeURIComponent(selected)}`)
      .then((d) => live && setDetail(d));
    return () => { live = false; };
  }, [selected, refresh]);
  return detail;
}

export function SkillsView({ onUse, initialBucket = null }: {
  onUse: (name: string) => void; initialBucket?: string | null;
}) {
  const [refresh, setRefresh] = useState(0);
  const list = usePoll<SkillsResponse>(`/v1/skills?r=${refresh}`, 60000);
  const [bucket, setBucket] = useState<string | null>(initialBucket);
  const [selected, setSelected] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<Draft | null>(null);
  const detail = useDetail(selected, refresh);

  const all = list?.skills ?? [];
  const buckets = list?.buckets ?? [];
  const named = buckets.map((b) => b.name).filter((b) => b !== OTHER);
  const q = query.trim().toLowerCase();
  const hits = useMemo(() => all.filter((s) =>
    `${s.name} ${s.description}`.toLowerCase().includes(q)), [all, q]);
  const inBucket = all.filter((s) => (s.bucket ?? OTHER) === bucket);
  const crumbBucket = selected ? (all.find((s) => s.name === selected)?.bucket ?? bucket) : bucket;

  const home = () => { setEditing(null); setSelected(null); setBucket(null); setQuery(""); };
  const openBucket = (b: string | null) => { setEditing(null); setSelected(null); setQuery(""); setBucket(b); };
  const pick = (name: string) => { setEditing(null); setSelected(name); };
  const saved = (name: string) => { setEditing(null); setSelected(name); setRefresh((r) => r + 1); };
  const fresh = () => setEditing({ name: "", description: "", body: "",
    category: bucket && bucket !== OTHER ? bucket : "" });

  const body = editing ? (
    <SkillEditor draft={editing} buckets={named} onSaved={saved} onCancel={() => setEditing(null)} />
  ) : selected ? (
    detail ? <Detail detail={detail} onUse={onUse} onEdit={setEditing} /> : null
  ) : q ? (
    <>
      <SectionLabel>{hits.length} matching skills</SectionLabel>
      <SkillRows skills={hits} showBucket onPick={pick} />
    </>
  ) : bucket ? (
    <>
      <SectionLabel>{inBucket.length} skills</SectionLabel>
      <SkillRows skills={inBucket} showBucket={false} onPick={pick} />
    </>
  ) : list && all.length === 0 ? (
    <EmptyState>No skills found. A skill is a SKILL.md the model is told about.</EmptyState>
  ) : (
    <SkillTiles buckets={buckets} skills={all} onOpen={openBucket} />
  );

  return (
    <div className="flex-1 min-h-0 flex flex-col gap-4 overflow-y-auto pr-2">
      <div className="flex items-center gap-4 flex-wrap">
        <Crumbs bucket={crumbBucket} skill={selected} onHome={home} onBucket={() => openBucket(crumbBucket)} />
        <span className="ml-auto"><Button onClick={fresh}>new skill</Button></span>
      </div>
      {!bucket && !selected && !editing && (
        <input aria-label="search skills" value={query} placeholder={`search all ${all.length} skills`}
          onChange={(e) => setQuery(e.target.value)} className="input max-w-md" />
      )}
      {body}
    </div>
  );
}
