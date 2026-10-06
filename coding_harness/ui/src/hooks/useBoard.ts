// The cross-project session board: GET /v1/board?all=1 every few seconds.
// One poll feeds both the board tab and turn-end notifications, so a
// notification fires whichever view is open.

import type { BoardAll, BoardServer } from "@/lib/types";
import { usePoll } from "./usePoll";

export const BOARD_POLL_MS = 3000;

export function useBoard(intervalMs = BOARD_POLL_MS): BoardAll | null {
  return usePoll<BoardAll>("/v1/board?all=1", intervalMs);
}

// A card's identity across servers: session ids are only unique per server.
export function cardKey(origin: string | undefined, sessionId: string): string {
  return `${origin ?? ""}|${sessionId}`;
}

// The last path segment of a server's cwd, the project name on a card.
export function projectName(server: BoardServer): string {
  const parts = server.server.cwd.split(/[\\/]/).filter(Boolean);
  return parts[parts.length - 1] ?? server.server.cwd;
}
