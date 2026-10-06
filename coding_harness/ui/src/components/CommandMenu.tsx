// Slash-command menu over the composer: built-ins with one-line help, custom
// commands from .bjorn/commands, and skills as `/skill <name>`. The composer
// owns the keyboard; this renders the list and reports clicks.

export interface CommandRow {
  name: string;
  usage: string;
  help: string;
  kind: "builtin" | "custom" | "skill";
}

const MAX_ROWS = 12;

function subsequence(needle: string, hay: string): boolean {
  let i = 0;
  for (const ch of hay) if (ch === needle[i]) i += 1;
  return i === needle.length;
}

/** Rows matching `query` (text after "/"), prefix matches before loose ones. */
export function filterCommands(rows: CommandRow[], query: string): CommandRow[] {
  const q = query.toLowerCase();
  const scored: Array<[number, CommandRow]> = [];
  for (const r of rows) {
    const name = r.name.slice(1).toLowerCase();
    if (name.startsWith(q)) scored.push([0, r]);
    else if (subsequence(q, name)) scored.push([1, r]);
  }
  return scored.sort((a, b) => a[0] - b[0]).slice(0, MAX_ROWS).map(([, r]) => r);
}

export function CommandMenu({ rows, active, onPick }: {
  rows: CommandRow[]; active: number; onPick: (row: CommandRow) => void;
}) {
  if (rows.length === 0) return null;
  return (
    <ul role="listbox" aria-label="commands"
      className="card absolute bottom-full left-0 right-0 mb-1 max-h-64 overflow-y-auto py-1 z-30 shadow-lg">
      {rows.map((r, i) => (
        <li key={r.name} role="option" aria-selected={i === active}
          onMouseDown={(e) => { e.preventDefault(); onPick(r); }}
          className={`px-3 py-1 cursor-pointer flex items-baseline gap-3 min-w-0 ${
            i === active ? "bg-accent/15" : "hover:bg-paper/60"}`}>
          <span className="font-mono text-[0.72rem] text-ink shrink-0">{r.name}</span>
          <span className="text-xs text-muted italic truncate">{r.help}</span>
          {r.kind !== "builtin" && <span className="label ml-auto shrink-0">{r.kind}</span>}
        </li>
      ))}
    </ul>
  );
}
