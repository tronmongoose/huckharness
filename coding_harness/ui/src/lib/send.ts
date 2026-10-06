// A write that keeps the server's error message. apiPost folds every failure
// into null; forms that show why a save failed need the message itself.

import { API_BASE } from "./api";

export interface SendResult<T> {
  data: T | null;
  error: string | null; // null on a 2xx
}

export async function sendJson<T>(method: string, path: string, body: unknown): Promise<SendResult<T>> {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = (await res.json().catch(() => null)) as (T & { error?: { message?: string } }) | null;
    if (res.ok) return { data, error: null };
    return { data, error: data?.error?.message ?? `request failed (${res.status})` };
  } catch {
    return { data: null, error: "the server did not answer" };
  }
}
