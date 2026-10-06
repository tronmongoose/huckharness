// How full the model's context window was on the last read. Past WARN_AT the
// bar turns accent and points at /compact.

import type { ContextUse } from "@/lib/types";

export const WARN_AT = 0.8;

function k(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

export function ContextMeter({ context }: { context: ContextUse | null }) {
  if (!context || context.max <= 0) return null;
  const share = Math.min(1, context.used / context.max);
  const warn = share >= WARN_AT;
  return (
    <span
      className="flex items-center gap-2"
      title={warn ? "context nearly full: /compact folds older history" : "prompt size on the last model read"}
    >
      <span className="label">context</span>
      <span className="w-20 h-1.5 bg-rule rounded overflow-hidden" aria-hidden="true">
        <span
          className={`block h-full ${warn ? "bg-accent" : "bg-muted"}`}
          style={{ width: `${Math.round(share * 100)}%` }}
        />
      </span>
      <span className={`font-mono text-[0.65rem] ${warn ? "text-accent" : "text-muted"}`}>
        {k(context.used)} / {k(context.max)} ({Math.round(share * 100)}%)
      </span>
    </span>
  );
}
