// A fetch stub that answers by URL substring and records POST bodies.
import { vi } from "vitest";

export function routeFetch(routes: Record<string, unknown>) {
  const posts: Array<{ url: string; body: unknown }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "POST") posts.push({ url: String(url), body: JSON.parse(String(init.body ?? "{}")) });
    const key = Object.keys(routes).find((k) => String(url).includes(k));
    const payload = key ? routes[key] : {};
    return new Response(JSON.stringify(payload), { status: payload === null ? 500 : 200 });
  }));
  return posts;
}
