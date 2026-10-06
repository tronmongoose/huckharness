// Shown instead of the app when the server is bound off loopback and this
// browser has no session. The server names only the token file's parent
// directory and name, never the full path on the machine running it.

import { type FormEvent, useState } from "react";

import { login } from "@/lib/auth";

export function Login({ onSuccess, tokenHint = null }: {
  onSuccess: () => void;
  tokenHint?: string | null;
}) {
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!token.trim() || busy) return;
    setBusy(true);
    const out = await login(token);
    setBusy(false);
    if (out.ok) {
      setToken("");
      onSuccess();
      return;
    }
    setError(out.message ?? "Login failed.");
  };

  return (
    <main className="min-h-screen bg-paper text-ink flex items-center justify-center px-4">
      <form onSubmit={(e) => void submit(e)} className="w-full max-w-md flex flex-col gap-3">
        <h1 className="font-serif text-2xl">bjorn</h1>
        <p className="text-sm text-muted">
          This server is reachable from the network. Paste the token from{" "}
          <code className="font-mono">{tokenHint ? `…/${tokenHint}` : "the auth_token file"}</code> on
          the machine running it.
        </p>
        <label className="label" htmlFor="auth-token">token</label>
        <input
          id="auth-token"
          type="password"
          autoComplete="current-password"
          autoFocus
          spellCheck={false}
          value={token}
          onChange={(e) => setToken(e.target.value)}
          className="input bg-card font-mono"
        />
        <button type="submit" className="btn" disabled={busy || !token.trim()}>
          {busy ? "checking…" : "log in"}
        </button>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
      </form>
    </main>
  );
}
