import { fireEvent, render, screen } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { COMMAND_MENU_EVENT } from "@/hooks/useShortcuts";
import { routeFetch } from "@/test/route-fetch";
import { Composer, mentionQuery, slashQuery } from "./Composer";
import { Thread } from "./Thread";

afterEach(() => vi.unstubAllGlobals());

const COMMANDS = {
  commands: [
    { name: "/model", usage: "/model <tag|auto>", help: "switch model", kind: "builtin" },
    { name: "/new", usage: "/new", help: "fresh session", kind: "builtin" },
    { name: "/skill demo-film", usage: "/skill demo-film <task>", help: "film it", kind: "skill" },
  ],
};

function Harness({ onSend = () => {}, initial = "" }: { onSend?: () => void; initial?: string }) {
  const [draft, setDraft] = useState(initial);
  const ref = useRef<HTMLTextAreaElement>(null);
  return (
    <>
      <Composer draft={draft} setDraft={setDraft} onSend={onSend} onStop={() => {}}
        busy={false} disabled={false} placeholder="" inputRef={ref} />
      <output data-testid="draft">{draft}</output>
    </>
  );
}

const box = () => screen.getByRole("textbox");
const draft = () => screen.getByTestId("draft").textContent;

describe("Composer", () => {
  it("finds the query being typed", () => {
    expect(mentionQuery("read @src/A")).toBe("src/A");
    expect(mentionQuery("mail me@host")).toBeNull();
    expect(slashQuery("/mo")).toBe("mo");
    expect(slashQuery("/skill dem")).toBe("skill dem");
    expect(slashQuery("/model gpt-oss:20b")).toBeNull();
    expect(slashQuery("hi /mo")).toBeNull();
  });

  it("@ opens the file picker and a pick inserts the path", async () => {
    routeFetch({ "/v1/files": { files: ["src/App.tsx", "src/lib/api.ts"] } });
    render(<Harness />);
    fireEvent.change(box(), { target: { value: "look at @ap" } });
    const option = await screen.findByRole("option", { name: "src/App.tsx" });
    expect(option).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(box(), { key: "ArrowDown" });
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(draft()).toBe("look at @src/lib/api.ts ");
    expect(screen.queryByRole("listbox")).toBeNull();
    expect(screen.getByText("@src/lib/api.ts")).toBeInTheDocument();
  });

  it("shows no chip for a path the server never listed", async () => {
    routeFetch({ "/v1/files": { files: [] } });
    render(<Harness />);
    fireEvent.change(box(), { target: { value: "read @missing.py now" } });
    expect(screen.queryByText("@missing.py")).toBeNull();
  });

  it("asks the server with the typed text", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      calls.push(String(url));
      return new Response(JSON.stringify({ files: ["a.py"] }));
    }));
    render(<Harness />);
    fireEvent.change(box(), { target: { value: "@Sr/a" } });
    await screen.findByRole("option", { name: "a.py" });
    expect(calls.some((u) => u.endsWith("/v1/files?q=Sr%2Fa"))).toBe(true);
  });

  it("/ at the start opens the command menu and Enter picks", async () => {
    routeFetch({ "/v1/commands": COMMANDS });
    render(<Harness />);
    fireEvent.change(box(), { target: { value: "/mo" } });
    await screen.findByRole("option", { name: /\/model/ });
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(draft()).toBe("/model ");
  });

  it("Enter on a complete command name sends instead of re-inserting", async () => {
    routeFetch({ "/v1/commands": COMMANDS });
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    fireEvent.change(box(), { target: { value: "/new" } });
    await screen.findByRole("option", { name: /\/new/ });
    fireEvent.keyDown(box(), { key: "Enter" });
    expect(onSend).toHaveBeenCalled();
  });

  it("Esc closes the menu without sending", async () => {
    routeFetch({ "/v1/commands": COMMANDS });
    render(<Harness />);
    fireEvent.change(box(), { target: { value: "/" } });
    await screen.findByRole("listbox");
    fireEvent.keyDown(box(), { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("Cmd+K opens the menu and a pick prefixes the draft", async () => {
    routeFetch({ "/v1/commands": COMMANDS });
    render(<Harness initial="the header" />);
    window.dispatchEvent(new Event(COMMAND_MENU_EVENT));
    await screen.findByRole("option", { name: /demo-film/ });
    fireEvent.mouseDown(screen.getByRole("option", { name: /demo-film/ }));
    expect(draft()).toBe("/skill demo-film the header");
  });

  it("Cmd+Enter sends even with a menu open", async () => {
    routeFetch({ "/v1/commands": COMMANDS });
    const onSend = vi.fn();
    render(<Harness onSend={onSend} />);
    fireEvent.change(box(), { target: { value: "/m" } });
    await screen.findByRole("listbox");
    fireEvent.keyDown(box(), { key: "Enter", metaKey: true });
    expect(onSend).toHaveBeenCalled();
  });

  it("existing /model still goes to the session inside Thread", async () => {
    const posts = routeFetch({ "/v1/sessions/s1/model": {} });
    render(<Thread sessionId="s1" thread={[]} pending={[]} turnInFlight={false}
      phase={{ kind: "idle", since: 0, detail: "", tokens: 0 }}
      disabled={false} onNewSession={() => {}} />);
    fireEvent.change(box(), { target: { value: "/model gpt-oss:20b" } });
    fireEvent.keyDown(box(), { key: "Enter" });
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ url: expect.stringContaining("/v1/sessions/s1/model"),
      body: { model: "gpt-oss:20b" } });
  });
});
