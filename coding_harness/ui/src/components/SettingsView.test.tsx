import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SettingsView } from "./SettingsView";

afterEach(() => vi.unstubAllGlobals());

const SETTINGS = {
  user: {
    model: "gemma3:12b",
    commandAllowlist: ["make test"],
    sandbox: { network: false },
    brain: { command: "recall", env: { TOKEN: "***" } },
    hooks: { PreToolUse: [] },
  },
  project: { denyWrite: ["secrets/**"] },
  effective: {},
  user_path: "/u/settings.json",
  project_path: "/p/.bjorn/settings.json",
  locked: ["brain", "hooks"],
  writable: [],
  errors: [],
  disabled: false,
};

function stub(putStatus: number, putBody: unknown, settings: object = SETTINGS) {
  const puts: unknown[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      puts.push(JSON.parse(String(init.body)));
      return new Response(JSON.stringify(putBody), { status: putStatus });
    }
    if (String(url).includes("/v1/models")) {
      return new Response(JSON.stringify({ models: [{ id: "gpt-oss:20b", backend: "ollama", text_only: false }] }));
    }
    return new Response(JSON.stringify(settings));
  }));
  return puts;
}

describe("SettingsView", () => {
  it("saves only the writable keys", async () => {
    const puts = stub(200, SETTINGS);
    render(<SettingsView />);
    fireEvent.change(await screen.findByLabelText("autonomy"), { target: { value: "medium" } });
    fireEvent.change(screen.getByLabelText("denyWrite"), { target: { value: "a/**\n\nb/**" } });
    fireEvent.click(screen.getByText("save"));
    await vi.waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({ user: {
      autonomy: "medium", model: "gemma3:12b", commandAllowlist: ["make test"],
      denyWrite: ["a/**", "b/**"],
    } });
    expect(screen.getByText(/apply to new sessions/)).toBeTruthy();
  });

  it("saves role models: code under model, the rest under models", async () => {
    const puts = stub(200, SETTINGS);
    render(<SettingsView />);
    fireEvent.change(await screen.findByLabelText("code model"), { target: { value: "gpt-oss:20b" } });
    fireEvent.change(screen.getByLabelText("chat model"), { target: { value: "gpt-oss:20b" } });
    fireEvent.click(screen.getByText("save"));
    await vi.waitFor(() => expect(puts).toHaveLength(1));
    expect(puts[0]).toEqual({ user: {
      model: "gpt-oss:20b", models: { chat: "gpt-oss:20b" }, commandAllowlist: ["make test"],
    } });
  });

  it("shows the server's validation error inline", async () => {
    stub(400, { error: { code: 400, message: "refusing to use model: banned" } });
    render(<SettingsView />);
    fireEvent.click(await screen.findByText("save"));
    expect((await screen.findByRole("alert")).textContent).toContain("refusing to use model");
  });

  it("shows locked keys and the project file read-only", async () => {
    stub(200, SETTINGS);
    render(<SettingsView />);
    expect(await screen.findByText(/Edit ~\/.config\/bjorn\/settings.json by hand/)).toBeTruthy();
    expect(screen.getByText(/secrets\/\*\*/)).toBeTruthy();
    expect(screen.queryByLabelText("brain")).toBeNull();
  });

  it("says when a save was written but not applied", async () => {
    stub(200, { saved: true, applied: false, error: "project file: bad key" });
    render(<SettingsView />);
    fireEvent.click(await screen.findByText("save"));
    expect((await screen.findByRole("alert")).textContent).toContain("not applied: project file: bad key");
  });

  it("disables save when the server ignores settings files", async () => {
    const puts = stub(200, SETTINGS, { ...SETTINGS, disabled: true });
    render(<SettingsView />);
    expect(await screen.findByText(/HARNESS_SETTINGS=off/)).toBeTruthy();
    const save = screen.getByText("save") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    fireEvent.click(save);
    expect(puts).toHaveLength(0);
  });

  it("shows sandbox read-only with the locked keys", async () => {
    stub(200, SETTINGS);
    render(<SettingsView />);
    expect(await screen.findByText(/Locked: brain, hooks, sandbox/)).toBeTruthy();
    expect(screen.getByText(/"network": false/)).toBeTruthy();
  });
});
