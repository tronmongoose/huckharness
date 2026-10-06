// The project this tab works in, and the switch to another. Each project runs
// its own server (tools read the process cwd), so switching moves the tab.

import { useState } from "react";

import { usePoll } from "@/hooks/usePoll";
import { apiPost } from "@/lib/api";

interface Project {
  name: string;
  path: string;
  current: boolean;
  url: string | null;
}

export function ProjectPicker({ cwd }: { cwd: string | null }) {
  const res = usePoll<{ projects: Project[] }>("/v1/projects", 20000);
  const [status, setStatus] = useState<string | null>(null);
  const projects = res?.projects ?? [];
  const current = projects.find((p) => p.current);
  // A worktree or a repo outside ~/projects is served but not listed.
  const here = current?.path ?? cwd ?? "";

  const open = async (path: string) => {
    if (path === here) return;
    setStatus("starting…");
    const out = await apiPost<{ url: string }>("/v1/projects/open", { path });
    if (!out) {
      setStatus("could not start it");
      return;
    }
    window.location.href = `${out.url}/?new=1`;
  };

  return (
    <span className="flex items-center gap-2">
      <select
        aria-label="project"
        value={here}
        onChange={(e) => void open(e.target.value)}
        className="input bg-card w-auto py-1 font-mono text-xs"
        title={here}
      >
        {!current && here && <option value={here}>{here.split("/").slice(-1)[0]}</option>}
        {projects.map((p) => (
          <option key={p.path} value={p.path}>
            {p.name}
            {p.url && !p.current ? "  · running" : ""}
          </option>
        ))}
      </select>
      {status && <span className="text-xs text-muted italic">{status}</span>}
    </span>
  );
}
