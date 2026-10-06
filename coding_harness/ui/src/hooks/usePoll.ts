// Interval polling hook (pattern forked from nemoclaw-smb ui/src/hooks/usePoll.ts).
// Fail-soft: a missed fetch keeps the last good value.

import { useEffect, useState } from "react";

import { apiFetch } from "@/lib/api";

export function usePoll<T>(path: string | null, intervalMs: number): T | null {
  const [data, setData] = useState<T | null>(null);

  useEffect(() => {
    // Clear immediately on any path change so a stale value from the previous
    // path is never shown against the new one (a revoked session's envelope
    // must not bleed onto the next session and lock its composer).
    setData(null);
    if (!path) return;
    // Per-effect cancel flag (not a shared ref): a single boolean ref is reset
    // to true by the next effect's setup before the previous path's in-flight
    // fetch resolves, so the old tick would land data under the new path.
    let cancelled = false;
    const tick = async () => {
      const res = await apiFetch<T>(path);
      if (!cancelled && res !== null) setData(res);
    };
    void tick();
    const t = setInterval(tick, intervalMs);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [path, intervalMs]);

  return data;
}
