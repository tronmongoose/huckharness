// Turn-end and needs-approval browser notifications, diffed from consecutive
// board polls. Fires only while the tab is hidden or the session is not the
// open one, and only once permission is granted. No sound.

import { useEffect, useRef, useState } from "react";

import type { BoardAll, BoardRow } from "@/lib/types";
import { cardKey, projectName } from "./useBoard";

export interface BoardNotice {
  key: string;
  title: string;
  body: string;
}

type Indexed = Map<string, { row: BoardRow; project: string }>;

function index(board: BoardAll): Indexed {
  const out: Indexed = new Map();
  for (const server of board.servers) {
    for (const row of server.sessions) {
      out.set(cardKey(server.origin, row.session_id), { row, project: projectName(server) });
    }
  }
  return out;
}

// What changed between two polls that is worth a notification.
// A turn counts as ended when a live status settles, or when the turn number
// moved on to a settled one: a turn shorter than the poll never shows running.
function transition(before: BoardRow, after: BoardRow): string | null {
  const settled = after.status === "done" || after.status === "error";
  const wasLive = before.status === "running" || before.status === "needs_approval";
  if (settled && (wasLive || after.turn > before.turn)) {
    return after.status === "error" ? "turn failed" : "turn finished";
  }
  if (before.pending === 0 && after.pending > 0) return "needs approval";
  return null;
}

// Title plus excerpt, without saying the prompt twice when the excerpt is the prompt.
function noticeBody(row: BoardRow): string {
  const title = row.title ?? "";
  const excerpt = row.last_excerpt ?? "";
  const parts = excerpt && title && excerpt.startsWith(title) ? [excerpt] : [title, excerpt];
  return parts.filter(Boolean).join("\n").slice(0, 240);
}

// Notices for rows that crossed a transition; quiet(key) suppresses one.
export function boardNotices(
  prev: BoardAll | null, next: BoardAll, quiet: (key: string) => boolean,
): BoardNotice[] {
  if (!prev) return [];
  const before = index(prev);
  const out: BoardNotice[] = [];
  for (const [key, { row, project }] of index(next)) {
    const was = before.get(key);
    const what = was ? transition(was.row, row) : null;
    if (!what || quiet(key)) continue;
    out.push({
      key,
      title: `${project}: ${what}`,
      body: noticeBody(row),
    });
  }
  return out;
}

export type NotifyState = NotificationPermission | "unsupported";

export function notifyState(): NotifyState {
  return typeof Notification === "undefined" ? "unsupported" : Notification.permission;
}

// The permission state and a request function; the request must run in a click handler.
export function useNotifyPermission(): [NotifyState, () => void] {
  const [state, setState] = useState<NotifyState>(notifyState);
  const ask = () => {
    if (typeof Notification === "undefined") return;
    void Notification.requestPermission().then(setState);
  };
  return [state, ask];
}

// Diff each new board against the previous one and notify on transitions.
export function useNotifications(board: BoardAll | null, activeId: string | null): void {
  const prev = useRef<BoardAll | null>(null);
  useEffect(() => {
    if (!board) return;
    const before = prev.current;
    prev.current = board;
    if (notifyState() !== "granted") return;
    const self = board.servers.find((s) => s.self);
    const active = activeId && self ? cardKey(self.origin, activeId) : null;
    const quiet = (key: string) => !document.hidden && key === active;
    for (const n of boardNotices(before, board, quiet)) {
      new Notification(n.title, { body: n.body, tag: n.key, silent: true });
    }
  }, [board, activeId]);
}
