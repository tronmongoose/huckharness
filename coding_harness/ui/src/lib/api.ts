// Fail-soft fetch wrapper (pattern forked from nemoclaw-smb ui/src/lib/api.ts).
// Network or non-2xx returns null — panels render empty states, never crash.

// Relative in a build, because `bjorn serve` now serves this bundle itself:
// requests follow whatever origin loaded the page, so reaching the harness
// over Tailscale works without the phone calling its own loopback. The dev
// server is the one case that needs an absolute base — it hosts the page on
// :5173 while the API stays on :9100. VITE_API_BASE overrides either.
export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ??
  (import.meta.env.DEV ? "http://127.0.0.1:9100" : "");

// A 401 means the server wants a login (non-loopback bind, see lib/auth.ts).
// Listeners flip the app to the login screen; the call itself still fails soft.
const unauthorizedListeners = new Set<() => void>();

export function onUnauthorized(cb: () => void): () => void {
  unauthorizedListeners.add(cb);
  return () => {
    unauthorizedListeners.delete(cb);
  };
}

export function notifyUnauthorized(): void {
  unauthorizedListeners.forEach((cb) => cb());
}

export async function apiFetch<T>(path: string): Promise<T | null> {
  try {
    const res = await fetch(`${API_BASE}${path}`, { credentials: "same-origin" });
    if (res.status === 401) notifyUnauthorized();
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

export async function apiPost<T>(
  path: string,
  body?: unknown,
): Promise<T | null> {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 401) notifyUnauthorized();
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}
