// Settings tab: the writable keys of the user settings file as a form. brain
// and hooks name commands the server spawns and sandbox is passed through
// unchecked, so they stay hand-edited and show read-only here, as does the
// whole project file. Saves apply to new sessions.

import { useCallback, useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";
import { AUTONOMY_LEVELS } from "@/lib/autonomy";
import { sendJson } from "@/lib/send";
import type { SettingsFile, SettingsResponse } from "@/lib/types";
import { ModelRoles } from "./ModelRoles";
import { Button, EmptyState, SectionLabel } from "./shared";

const LIST_FIELDS = [
  { key: "commandAllowlist", hint: "command prefixes allowed from LOW up" },
  { key: "commandDenylist", hint: "command prefixes denied at every level" },
  { key: "commandBlocklist", hint: "command prefixes that never run (adds to the built-in list)" },
  { key: "denyWrite", hint: "globs no tool may write" },
  { key: "extraReadRoots", hint: "directories readable outside the project" },
] as const;

interface Form {
  autonomy: string;
  model: string; // the code role, stored under the legacy `model` key
  roles: Record<string, string>; // every other role, stored under `models`
  lists: Record<string, string>; // one entry per line
}

function rolesOf(models: unknown): Record<string, string> {
  if (!models || typeof models !== "object") return {};
  return Object.fromEntries(Object.entries(models as Record<string, unknown>)
    .filter(([k, v]) => k !== "code" && typeof v === "string") as Array<[string, string]>);
}

function toForm(user: SettingsFile): Form {
  const lists: Record<string, string> = {};
  for (const f of LIST_FIELDS) {
    const v = user[f.key];
    lists[f.key] = Array.isArray(v) ? v.join("\n") : "";
  }
  return {
    autonomy: typeof user.autonomy === "string" ? user.autonomy : "",
    model: typeof user.model === "string" ? user.model : "",
    roles: rolesOf(user.models),
    lists,
  };
}

// Shown read-only; the server keeps them from disk on every save.
const HAND_EDITED = ["brain", "hooks", "sandbox"];

// The PUT body: writable keys only, empty values left out so they clear.
function toPayload(form: Form): SettingsFile {
  const out: SettingsFile = {};
  if (form.autonomy) out.autonomy = form.autonomy;
  if (form.model.trim()) out.model = form.model.trim();
  const roles = Object.fromEntries(Object.entries(form.roles).filter(([, v]) => v));
  if (Object.keys(roles).length) out.models = roles;
  for (const f of LIST_FIELDS) {
    const items = form.lists[f.key].split("\n").map((s) => s.trim()).filter(Boolean);
    if (items.length) out[f.key] = items;
  }
  return out;
}

// Whether the file was written, and what to tell the operator if anything.
async function putSettings(user: SettingsFile): Promise<{ written: boolean; problem: string | null }> {
  const { data, error } = await sendJson<{ applied?: boolean; error?: string }>(
    "PUT", "/v1/settings", { user });
  if (error) return { written: false, problem: error };
  if (data?.applied === false) {
    return { written: true, problem: `saved, but not applied: ${data.error ?? "the merged settings are invalid"}` };
  }
  return { written: true, problem: null };
}

function Json({ value }: { value: unknown }) {
  return (
    <pre className="font-mono text-[0.7rem] whitespace-pre-wrap break-all border border-rule rounded p-2 text-muted">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function Locked({ data }: { data: SettingsResponse }) {
  const locked = Object.fromEntries(HAND_EDITED.filter((k) => k in data.user).map((k) => [k, data.user[k]]));
  return (
    <div className="space-y-4">
      <div>
        <SectionLabel>Locked: {HAND_EDITED.join(", ")}</SectionLabel>
        <p className="text-xs text-muted mb-2">
          brain and hooks name commands the server spawns, and sandbox is passed through
          unchecked. Edit ~/.config/bjorn/settings.json by hand.
        </p>
        {Object.keys(locked).length ? <Json value={locked} /> : <EmptyState>not set</EmptyState>}
      </div>
      <div>
        <SectionLabel>Project file (read-only)</SectionLabel>
        <p className="font-mono text-[0.65rem] text-muted mb-2 break-all">{data.project_path}</p>
        {Object.keys(data.project).length ? <Json value={data.project} /> : <EmptyState>no project settings</EmptyState>}
      </div>
    </div>
  );
}

function Fields({ form, setForm }: { form: Form; setForm: (f: Form) => void }) {
  return (
    <div className="space-y-4">
      <label className="block">
        <span className="label block mb-1">autonomy</span>
        <select aria-label="autonomy" value={form.autonomy} className="input"
          onChange={(e) => setForm({ ...form, autonomy: e.target.value })}>
          <option value="">server default (low)</option>
          {AUTONOMY_LEVELS.map((l) => <option key={l.key} value={l.key}>{l.key}: {l.hint}</option>)}
        </select>
      </label>
      <div>
        <span className="label block mb-2">models by role</span>
        <ModelRoles
          values={{ ...form.roles, code: form.model }}
          onChange={(role, tag) => role === "code"
            ? setForm({ ...form, model: tag })
            : setForm({ ...form, roles: { ...form.roles, [role]: tag } })}
        />
      </div>
      {LIST_FIELDS.map((f) => (
        <label key={f.key} className="block">
          <span className="label block mb-1">{f.key}</span>
          <span className="text-xs text-muted block mb-1">{f.hint}, one per line</span>
          <textarea aria-label={f.key} rows={3} value={form.lists[f.key]} className="input font-mono text-xs"
            onChange={(e) => setForm({ ...form, lists: { ...form.lists, [f.key]: e.target.value } })} />
        </label>
      ))}
    </div>
  );
}

export function SettingsView() {
  const [data, setData] = useState<SettingsResponse | null>(null);
  const [form, setForm] = useState<Form | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const load = useCallback(async () => {
    const res = await apiFetch<SettingsResponse>("/v1/settings");
    if (res) { setData(res); setForm(toForm(res.user)); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  if (!data || !form) return <EmptyState>loading settings</EmptyState>;

  const save = async () => {
    setSaved(false);
    const { written, problem } = await putSettings(toPayload(form));
    setError(problem);
    setSaved(written && problem === null);
    if (written) void load();
  };

  return (
    <section className="flex-1 overflow-y-auto min-h-0 grid gap-8 md:grid-cols-2 max-w-5xl">
      <div className="space-y-4">
        <SectionLabel>User file</SectionLabel>
        <p className="font-mono text-[0.65rem] text-muted break-all">{data.user_path}</p>
        {data.errors.map((e) => <p key={e} className="text-xs text-danger">{e}</p>)}
        {data.disabled && (
          <p className="text-xs text-danger">
            HARNESS_SETTINGS=off: this server ignores the settings files, so saving is disabled.
          </p>
        )}
        <Fields form={form} setForm={(f) => { setForm(f); setSaved(false); }} />
        <div className="flex items-center gap-3">
          <Button onClick={() => void save()} disabled={data.disabled}>save</Button>
          <span className="text-xs text-muted">Saved settings apply to new sessions.</span>
        </div>
        {error && <p role="alert" className="text-xs text-danger break-words">{error}</p>}
        {saved && <p className="text-xs text-muted">saved</p>}
      </div>
      <Locked data={data} />
    </section>
  );
}
