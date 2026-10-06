import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { BoardAll, BoardRow, BoardStatus } from "@/lib/types";
import { boardNotices, useNotifications } from "./useNotifications";

const ORIGIN = "http://127.0.0.1:1";

function boardOf(
  status: BoardStatus, pending = 0, id = "s1", over: Partial<BoardRow> = {},
): BoardAll {
  const row: BoardRow = {
    session_id: id, title: "fix it", status, pending, last_excerpt: "all green",
    last_event_ts: 1, turn: 1, model: "m", autonomy: "low", ...over,
  };
  return {
    servers: [{ server: { cwd: "/p/alpha", port: 1, pid: 1 }, origin: ORIGIN, self: true, sessions: [row] }],
    errors: [],
  };
}

function fakeNotification(permission: NotificationPermission) {
  const fired: Array<{ title: string; body?: string }> = [];
  class FakeNotification {
    static permission = permission;
    static requestPermission = vi.fn(async () => permission);
    constructor(title: string, opts?: NotificationOptions) {
      fired.push({ title, body: opts?.body });
    }
  }
  vi.stubGlobal("Notification", FakeNotification);
  return fired;
}

function setHidden(hidden: boolean) {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
}

afterEach(() => {
  vi.unstubAllGlobals();
  setHidden(false);
});

// Render the hook across a sequence of polls; the fired notifications.
function poll(boards: BoardAll[], activeId: string | null, permission: NotificationPermission = "granted") {
  const fired = fakeNotification(permission);
  const { rerender } = renderHook(({ b }) => useNotifications(b, activeId), {
    initialProps: { b: boards[0] },
  });
  for (const b of boards.slice(1)) rerender({ b });
  return fired;
}

describe("boardNotices", () => {
  const never = () => false;
  it("notices running to done or error and pending rising from zero", () => {
    expect(boardNotices(boardOf("running"), boardOf("done"), never)[0].title).toBe("alpha: turn finished");
    expect(boardNotices(boardOf("running"), boardOf("error"), never)[0].title).toBe("alpha: turn failed");
    expect(boardNotices(boardOf("running"), boardOf("needs_approval", 1), never)[0].title)
      .toBe("alpha: needs approval");
  });

  it("notices a turn that ended between polls and approval settling to done", () => {
    const next = boardOf("done", 0, "s1", { turn: 2 });
    expect(boardNotices(boardOf("done"), next, never)[0].title).toBe("alpha: turn finished");
    expect(boardNotices(boardOf("needs_approval", 1), boardOf("done"), never)[0].title)
      .toBe("alpha: turn finished");
  });

  it("does not repeat the prompt when the excerpt is the prompt", () => {
    const next = boardOf("error", 0, "s1", { last_excerpt: "fix it" });
    expect(boardNotices(boardOf("running"), next, never)[0].body).toBe("fix it");
  });

  it("ignores other transitions and the first poll", () => {
    expect(boardNotices(null, boardOf("done"), never)).toEqual([]);
    expect(boardNotices(boardOf("idle"), boardOf("running"), never)).toEqual([]);
    expect(boardNotices(boardOf("done"), boardOf("done"), never)).toEqual([]);
    expect(boardNotices(boardOf("needs_approval", 1), boardOf("needs_approval", 2), never)).toEqual([]);
  });
});

describe("useNotifications", () => {
  it("fires for the open session only while the tab is hidden", () => {
    expect(poll([boardOf("running"), boardOf("done")], "s1")).toEqual([]);
    setHidden(true);
    const fired = poll([boardOf("running"), boardOf("done")], "s1");
    expect(fired).toEqual([{ title: "alpha: turn finished", body: "fix it\nall green" }]);
  });

  it("fires for an inactive session while visible", () => {
    const fired = poll([boardOf("running"), boardOf("running", 1)], "other");
    expect(fired.map((n) => n.title)).toEqual(["alpha: needs approval"]);
  });

  it("stays silent without permission", () => {
    setHidden(true);
    expect(poll([boardOf("running"), boardOf("done")], null, "default")).toEqual([]);
  });
});
