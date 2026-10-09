// The skill editor: name, one-line description, optional bucket, and body.
// Saves through POST /v1/skills/<name>; the server writes the frontmatter.

import { useState } from "react";

import { apiPost } from "@/lib/api";
import { Button, TextAction } from "./shared";

export interface Draft {
  name: string;
  description: string;
  body: string;
  category: string;
}

// Frontmatter off, so the editor holds only what the person wrote.
export function skillBody(text: string): string {
  return text.replace(/^---\n[\s\S]*?\n---\n+/, "").trim();
}

export function SkillEditor({ draft, buckets, onSaved, onCancel }: {
  draft: Draft; buckets: string[]; onSaved: (name: string) => void; onCancel: () => void;
}) {
  const [d, setD] = useState(draft);
  const [error, setError] = useState<string | null>(null);
  const save = async () => {
    setError(null);
    const payload = { description: d.description, body: d.body,
      ...(d.category ? { category: d.category } : {}) };
    const res = await apiPost(`/v1/skills/${encodeURIComponent(d.name)}`, payload);
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
      <select aria-label="skill bucket" value={d.category}
        onChange={(e) => setD({ ...d, category: e.target.value })} className="input bg-card">
        <option value="">bucket: automatic</option>
        {buckets.map((b) => <option key={b} value={b}>{b}</option>)}
      </select>
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
