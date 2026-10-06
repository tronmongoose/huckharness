// GUI login for a `bjorn serve --host <non-loopback>` server. The server keeps
// the session in an HttpOnly cookie, so this module never sees or stores it:
// it asks /v1/auth/status, posts the pasted token, and listens for any 401.

import { useCallback, useEffect, useState } from "react";

import { API_BASE, onUnauthorized } from "./api";

export type AuthState = "checking" | "open" | "login";

export interface LoginResult {
  ok: boolean;
  message?: string;
}

interface Status {
  required: boolean;
  authenticated: boolean;
  token_path_hint?: string;
}

export interface AuthStatus {
  state: AuthState;
  tokenHint: string | null;
}

// A failed status call leaves the app open: the server still enforces, and
// the first 401 from any panel flips to the login screen.
export async function fetchAuthState(): Promise<AuthStatus> {
  try {
    const res = await fetch(`${API_BASE}/v1/auth/status`, { credentials: "same-origin" });
    if (!res.ok) return { state: "open", tokenHint: null };
    const s = (await res.json()) as Status;
    return {
      state: s.required && !s.authenticated ? "login" : "open",
      tokenHint: typeof s.token_path_hint === "string" ? s.token_path_hint : null,
    };
  } catch {
    return { state: "open", tokenHint: null };
  }
}

export async function login(token: string): Promise<LoginResult> {
  try {
    const res = await fetch(`${API_BASE}/v1/auth`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ token: token.trim() }),
    });
    if (res.ok) return { ok: true };
    const body = (await res.json().catch(() => ({}))) as {
      retry_after?: number;
      attempts_left?: number;
    };
    if (res.status === 429) {
      return { ok: false, message: `Too many attempts. Locked out, try again in ${body.retry_after ?? 60} s.` };
    }
    if (res.status === 401) {
      const left = body.attempts_left;
      if (left === 0) return { ok: false, message: "Wrong token. Locked out for 60 s." };
      return { ok: false, message: left === undefined ? "Wrong token." : `Wrong token. ${left} attempts left.` };
    }
    return { ok: false, message: `Login failed (${res.status}).` };
  } catch {
    return { ok: false, message: "Cannot reach the server." };
  }
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API_BASE}/v1/auth/logout`, { method: "POST", credentials: "same-origin" });
  } catch {
    // The server drops the session on restart anyway.
  }
}

export function useAuth(): { state: AuthState; tokenHint: string | null; recheck: () => void } {
  const [state, setState] = useState<AuthState>("checking");
  const [tokenHint, setTokenHint] = useState<string | null>(null);
  const recheck = useCallback(() => {
    void fetchAuthState().then((s) => {
      setState(s.state);
      if (s.tokenHint) setTokenHint(s.tokenHint);
    });
  }, []);
  useEffect(() => {
    recheck();
    return onUnauthorized(() => setState("login"));
  }, [recheck]);
  return { state, tokenHint, recheck };
}
