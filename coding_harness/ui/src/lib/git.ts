// Git panel calls. POSTs keep the server's error text, which apiPost
// collapses to null: a refused commit must say why.

import { API_BASE } from "./api";

export interface PostResult<T> {
  ok: boolean;
  data: T | null;
  error: string | null;
}

export async function postJson<T>(path: string, body: unknown): Promise<PostResult<T>> {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = (await res.json().catch(() => null)) as
      (T & { error?: { message?: string } }) | null;
    if (res.ok) return { ok: true, data, error: null };
    return { ok: false, data: null, error: data?.error?.message ?? `HTTP ${res.status}` };
  } catch {
    return { ok: false, data: null, error: "server unreachable" };
  }
}
