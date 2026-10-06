// Which model does which job. Code writes the legacy `model` key; the other
// roles write the `models` block. Empty means the role's default, shown
// with where the current choice came from (default, settings, env).

import { usePoll } from "@/hooks/usePoll";
import type { ModelsResponse } from "@/lib/types";
import { capability } from "./Header";

export const ROLES = [
  { key: "code", hint: "turns that use tools and edit files" },
  { key: "chat", hint: "chat sessions: no tools, quick answers" },
  { key: "explore", hint: "the read-only research subagent" },
  { key: "review", hint: "grades a change before it is done" },
  { key: "summarize", hint: "folds long history, drafts memory proposals" },
] as const;

interface RoleRow {
  role: string;
  model: string;
  source: string;
}

export function ModelRoles({ values, onChange }: {
  values: Record<string, string>; onChange: (role: string, tag: string) => void;
}) {
  const models = usePoll<ModelsResponse>("/v1/models", 60000);
  const current = usePoll<{ roles: RoleRow[] }>("/v1/models/roles", 30000);
  const now = Object.fromEntries((current?.roles ?? []).map((r) => [r.role, r]));
  const local = (models?.models ?? []).filter((m) => m.backend === "ollama");
  return (
    <div className="space-y-3">
      {ROLES.map(({ key, hint }) => {
        const value = values[key] ?? "";
        const ids = local.map((m) => m.id);
        return (
          <label key={key} className="block">
            <span className="flex items-baseline gap-2">
              <span className="label">{key}</span>
              <span className="text-xs text-muted">{hint}</span>
            </span>
            <select aria-label={`${key} model`} value={value} className="input mt-1"
              onChange={(e) => onChange(key, e.target.value)}>
              <option value="">default</option>
              {value && !ids.includes(value) && <option value={value}>{value}</option>}
              {local.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}{capability(m) ? `  · ${capability(m)}` : ""}
                </option>
              ))}
            </select>
            {now[key] && (
              <span className="font-mono text-[0.62rem] text-muted">
                now {now[key].model} · {now[key].source}
              </span>
            )}
          </label>
        );
      })}
    </div>
  );
}
