// `@` file picker over the composer, fed by GET /v1/files?q=. The composer
// owns the keyboard; this renders the matches and reports clicks. Also the
// chips for files already mentioned in the draft, in AttachChips' look.

export function Mention({ files, active, onPick }: {
  files: string[]; active: number; onPick: (path: string) => void;
}) {
  if (files.length === 0) return null;
  return (
    <ul role="listbox" aria-label="files"
      className="card absolute bottom-full left-0 right-0 mb-1 max-h-64 overflow-y-auto py-1 z-30 shadow-lg">
      {files.map((f, i) => (
        <li key={f} role="option" aria-selected={i === active}
          onMouseDown={(e) => { e.preventDefault(); onPick(f); }}
          className={`px-3 py-1 cursor-pointer font-mono text-[0.72rem] break-all ${
            i === active ? "bg-accent/15 text-ink" : "text-muted hover:bg-paper/60"}`}>
          {f}
        </li>
      ))}
    </ul>
  );
}

const TOKEN = /(?:^|\s)@([^\s@]+)(?=\s)/g;

/** Distinct `@path` tokens in a draft, in order. */
export function mentionsIn(draft: string): string[] {
  const out: string[] = [];
  for (const m of draft.matchAll(TOKEN)) if (!out.includes(m[1])) out.push(m[1]);
  return out;
}

export function MentionChips({ draft, known, onRemove }: {
  draft: string; known: ReadonlySet<string>; onRemove: (path: string) => void;
}) {
  const paths = mentionsIn(draft).filter((p) => known.has(p));
  if (paths.length === 0) return null;
  return (
    <div className="flex flex-wrap items-center gap-2 pb-2">
      {paths.map((p) => (
        <span key={p} className="flex items-center gap-1.5 border border-rule rounded px-2 py-0.5 min-w-0">
          <span className="font-mono text-[0.68rem] text-ink break-all">@{p}</span>
          <button type="button" aria-label={`remove @${p}`} onClick={() => onRemove(p)}
            className="text-muted hover:text-danger text-xs">×</button>
        </span>
      ))}
    </div>
  );
}
