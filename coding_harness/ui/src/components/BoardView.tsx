// Board: every session on every running bjorn GUI server, one card each.
// A card on this server selects the session; one on another project's
// server opens that server's page on the session in a new tab.

import { useEffect, useState } from "react";

import { projectName } from "@/hooks/useBoard";
import type { BoardAll, BoardServer } from "@/lib/types";
import { EmptyState } from "./shared";
import { SessionCard } from "./SessionCard";

// Another server's page, opened on one session (App reads #s=<id>).
export function sessionUrl(origin: string, sessionId: string): string {
  return `${origin}/#s=${encodeURIComponent(sessionId)}`;
}

function useNow(ms: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}

export function BoardView({ board, onSelect }: {
  board: BoardAll | null;
  onSelect: (sessionId: string) => void;
}) {
  const now = useNow(1000);
  if (!board) return <EmptyState>Loading the board.</EmptyState>;
  const open = (server: BoardServer, id: string) => {
    if (server.self || !server.origin) onSelect(id);
    else window.open(sessionUrl(server.origin, id), "_blank", "noopener");
  };
  const cards = board.servers.flatMap((server) =>
    server.sessions.map((row) => ({ server, row, key: `${server.origin ?? ""}|${row.session_id}` })));
  return (
    <div className="flex-1 min-w-0 overflow-y-auto pr-2">
      <p className="text-sm text-muted italic mb-3">
        Every session on every open project. Click a card to open it.
      </p>
      {cards.length === 0 ? (
        <EmptyState>No sessions on any running server.</EmptyState>
      ) : (
        <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 xl:grid-cols-3">
          {cards.map(({ server, row, key }) => (
            <SessionCard key={key} project={projectName(server)} row={row} nowMs={now}
              onOpen={() => open(server, row.session_id)} />
          ))}
        </div>
      )}
      {board.errors.length > 0 && (
        <p className="label mt-4">
          unreachable: {board.errors.map((e) => e.origin).join(", ")}
        </p>
      )}
    </div>
  );
}
