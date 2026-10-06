// Routines: every heartbeat-scheduled job on the machine, with failure
// state. Read-only window — the scheduler is managed via make/launchctl.

import { usePoll } from "@/hooks/usePoll";
import type { RoutinesResponse } from "@/lib/types";
import { EmptyState, SectionLabel } from "./shared";

export function RoutinesView() {
  const data = usePoll<RoutinesResponse>("/v1/routines", 30000);

  if (!data) {
    return <EmptyState>loading routines…</EmptyState>;
  }
  if (data.error) {
    return <EmptyState>routines unavailable: {data.error}</EmptyState>;
  }

  return (
    <section className="flex-1 min-w-0 card p-5">
      <div className="flex items-center gap-4 mb-4">
        <SectionLabel>
          {data.total} scheduled jobs · {data.failed} failing
        </SectionLabel>
        <span className="flex items-center gap-1.5 ml-auto">
          <span
            className={`inline-block w-1.5 h-1.5 rounded-full ${
              data.scheduler_alive ? "bg-accent animate-heartbeat" : "bg-danger"
            }`}
          />
          <span className="label">
            scheduler {data.scheduler_alive ? "alive" : "down"}
          </span>
        </span>
      </div>
      <div className="overflow-y-auto max-h-[70vh]">
        <table className="w-full text-left">
          <thead>
            <tr className="border-b border-rule">
              <th className="label font-normal py-2 pr-4">job</th>
              <th className="label font-normal py-2 pr-4">cadence</th>
              <th className="label font-normal py-2 pr-4">status</th>
              <th className="label font-normal py-2">detail</th>
            </tr>
          </thead>
          <tbody>
            {data.jobs.map((j) => (
              <tr key={j.id} className="border-b border-rule/50 align-top">
                <td className="py-2 pr-4">
                  <span className="font-mono text-xs text-ink">{j.id}</span>
                  <span className="block text-[0.7rem] text-muted max-w-xs truncate">
                    {j.description}
                  </span>
                </td>
                <td className="py-2 pr-4 font-mono text-xs text-muted whitespace-nowrap">
                  {j.cadence}
                </td>
                <td
                  className={`py-2 pr-4 font-mono text-xs ${
                    j.status === "failed" ? "text-danger" : "text-muted"
                  }`}
                >
                  {j.status === "failed"
                    ? `failed ×${j.consecutive_failures}`
                    : "ok"}
                </td>
                <td className="py-2 text-[0.7rem] text-muted max-w-sm">
                  {j.status === "failed" ? (
                    <span className="text-danger/80" title={j.last_error ?? ""}>
                      {(j.last_error ?? "").slice(0, 90)}
                    </span>
                  ) : (
                    <span className="truncate block" title={j.command}>
                      {j.command}
                    </span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
