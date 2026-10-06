import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { apiFetch, onUnauthorized } from "@/lib/api";
import { useAuth } from "@/lib/auth";

import { Login } from "./Login";
import { SpecPanel } from "./SpecPanel";

afterEach(() => vi.unstubAllGlobals());

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status });
}

function Gate() {
  const auth = useAuth();
  if (auth.state === "checking") return null;
  if (auth.state === "login") return <Login onSuccess={auth.recheck} tokenHint={auth.tokenHint} />;
  return <p>workspace</p>;
}

describe("Login", () => {
  it("renders when the server requires a login and posts the pasted token", async () => {
    let authed = false;
    const posts: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (String(url).endsWith("/v1/auth/status")) return json({ required: true, authenticated: authed });
      posts.push(JSON.parse(String(init?.body)));
      expect(init?.credentials).toBe("same-origin");
      authed = true;
      return json({ ok: true, required: true });
    }));
    render(<Gate />);
    fireEvent.change(await screen.findByLabelText("token"), { target: { value: "  s3cret \n" } });
    fireEvent.click(screen.getByRole("button", { name: "log in" }));
    expect(await screen.findByText("workspace")).toBeInTheDocument();
    expect(posts).toEqual([{ token: "s3cret" }]);
  });

  it("names the token file from the server's hint", async () => {
    vi.stubGlobal("fetch", vi.fn(async () =>
      json({ required: true, authenticated: false, token_path_hint: "secrets/tok" })));
    render(<Gate />);
    expect(await screen.findByText("…/secrets/tok")).toBeInTheDocument();
    expect(screen.queryByText(/\.config\/bjorn/)).toBeNull();
  });

  it("stays out of the way when no login is required", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ required: false, authenticated: true })));
    render(<Gate />);
    expect(await screen.findByText("workspace")).toBeInTheDocument();
  });

  it("shows the wrong-token and lockout messages", async () => {
    const replies = [
      json({ error: "unauthorized", attempts_left: 3 }, 401),
      json({ error: "locked", retry_after: 42 }, 429),
    ];
    vi.stubGlobal("fetch", vi.fn(async () => replies.shift()!));
    render(<Login onSuccess={() => {}} />);
    fireEvent.change(screen.getByLabelText("token"), { target: { value: "nope" } });
    fireEvent.click(screen.getByRole("button", { name: "log in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Wrong token. 3 attempts left.");
    fireEvent.click(screen.getByRole("button", { name: "log in" }));
    expect(await screen.findByText(/Locked out, try again in 42 s/)).toBeInTheDocument();
  });

  it("flips the open app to the login screen on any api 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) =>
      String(url).endsWith("/v1/auth/status")
        ? json({ required: false, authenticated: true })
        : json({ error: "unauthorized" }, 401)));
    render(<Gate />);
    expect(await screen.findByText("workspace")).toBeInTheDocument();
    let out: unknown = "unset";
    await act(async () => {
      out = await apiFetch("/v1/sessions");
    });
    expect(out).toBeNull();
    expect(await screen.findByLabelText("token")).toBeInTheDocument();
  });

  it("flips to login when the spec panel's own fetch gets a 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: "unauthorized" }, 401)));
    const seen = vi.fn();
    const off = onUnauthorized(seen);
    render(<SpecPanel sessionId="s1" turnInFlight={false}
      spec={{ version: 0, bytes: null, error: null }} todos={[]} />);
    await vi.waitFor(() => expect(seen).toHaveBeenCalled());
    off();
  });
});
