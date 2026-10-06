import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import type { GitStatus, PendingPermission } from "@/lib/types";
import { GitPanel, withRenameSources } from "./GitPanel";

describe("withRenameSources", () => {
  it("adds a ticked staged rename's old path once", () => {
    const staged = [{ path: "new.py", status: "R", orig: "old.py" }, { path: "k.py", status: "M" }];
    expect(withRenameSources(new Set(["new.py", "k.py"]), staged)).toEqual(["new.py", "k.py", "old.py"]);
    expect(withRenameSources(new Set(["k.py"]), staged)).toEqual(["k.py"]);
  });
});

afterEach(() => vi.unstubAllGlobals());

const status: GitStatus = {
  branch: "main", detached: false, head: "a".repeat(40), upstream: "origin/main",
  ahead: 2, behind: 1,
  staged: [{ path: "s.py", status: "M" }],
  unstaged: [{ path: "u.py", status: "M" }],
  untracked: [{ path: "n.py", status: "?" }],
};

const routes = (extra: Record<string, unknown> = {}) =>
  routeFetch({ "/v1/git/commit": { sha: "b".repeat(40), paths: ["u.py"], message_first_line: "m" },
    "/v1/worktrees": { worktrees: [] }, "/v1/git": status, ...extra });

describe("GitPanel", () => {
  it("shows branch and ahead/behind and the three lists", async () => {
    routes();
    render(<GitPanel sessionId="s1" turnInFlight={false} />);
    expect(await screen.findByText("main")).toBeInTheDocument();
    expect(screen.getByText(/origin\/main ↑2 ↓1/)).toBeInTheDocument();
    expect(screen.getByLabelText("staged s.py")).toBeInTheDocument();
    expect(screen.getByLabelText("unstaged u.py")).toBeInTheDocument();
    expect(screen.getByLabelText("untracked n.py")).toBeInTheDocument();
  });

  it("enables commit only with ticked paths and a message, and posts just those", async () => {
    const posts = routes();
    render(<GitPanel sessionId="s1" turnInFlight={false} />);
    await screen.findByText("main");
    const button = () => screen.getByRole("button", { name: /commit/ });
    expect(button()).toBeDisabled();
    fireEvent.click(screen.getByLabelText("unstaged u.py"));
    expect(button()).toBeDisabled();
    fireEvent.change(screen.getByLabelText("commit message"), { target: { value: "gui: x" } });
    expect(button()).toBeEnabled();
    fireEvent.click(screen.getByLabelText("untracked n.py"));
    fireEvent.click(screen.getByLabelText("untracked n.py"));
    fireEvent.click(button());
    await vi.waitFor(() => expect(posts.some((p) => p.url.includes("/v1/git/commit"))).toBe(true));
    const commit = posts.find((p) => p.url.includes("/v1/git/commit"));
    expect(commit?.body).toEqual({ session_id: "s1", message: "gui: x", paths: ["u.py"] });
    expect(await screen.findByText("committed bbbbbbbb")).toBeInTheDocument();
  });

  it("shows the server's reason when a commit is refused", async () => {
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return new Response(JSON.stringify({ error: { code: 403, message: "commit not approved (deny)" } }),
          { status: 403 });
      }
      const body = String(url).includes("/v1/worktrees") ? { worktrees: [] } : status;
      return new Response(JSON.stringify(body), { status: 200 });
    }));
    render(<GitPanel sessionId="s1" turnInFlight={false} />);
    await screen.findByText("main");
    fireEvent.click(screen.getByLabelText("staged s.py"));
    fireEvent.change(screen.getByLabelText("commit message"), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: /commit/ }));
    expect(await screen.findByText("commit not approved (deny)")).toBeInTheDocument();
  });

  it("disables commit mid-turn", async () => {
    routes();
    render(<GitPanel sessionId="s1" turnInFlight />);
    await screen.findByText("main");
    fireEvent.click(screen.getByLabelText("staged s.py"));
    fireEvent.change(screen.getByLabelText("commit message"), { target: { value: "x" } });
    expect(screen.getByRole("button", { name: /commit/ })).toBeDisabled();
  });

  it("renders a parked commit's permission card", async () => {
    routes();
    const req: PendingPermission = {
      req_id: "r1", tool: "GitCommit", reason: "git commit from the panel",
      args_preview: { command: "git --literal-pathspecs add -- u.py" },
      created_at: new Date().toISOString(), kind: "permission",
    };
    render(<GitPanel sessionId="s1" turnInFlight={false} pending={[req]} />);
    expect(await screen.findByText(/add -- u.py/)).toBeInTheDocument();
  });
});
