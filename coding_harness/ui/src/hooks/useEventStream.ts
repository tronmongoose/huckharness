// EventSource wrapper over GET /v1/sessions/{id}/events. Parses the JSON-RPC
// notifications and feeds lib/reducer. Reconnects with backoff; on every
// (re)connect it reconciles pending permissions from GET /permissions — the
// designed-for mitigation so a dropped SSE event costs poll latency, not a
// stuck JIT card.

import { useEffect, useReducer, useRef, useState } from "react";

import { API_BASE, apiFetch } from "@/lib/api";
import {
  initialStreamState,
  reconcilePending,
  reduceEvent,
  type StreamState,
} from "@/lib/reducer";
import type { PendingPermission, StreamEvent, TodoItem } from "@/lib/types";

type Action =
  | { kind: "event"; ev: StreamEvent }
  | { kind: "reconcile"; pending: PendingPermission[] }
  | { kind: "reset" };

function dispatchReduce(state: StreamState, action: Action): StreamState {
  switch (action.kind) {
    case "event":
      return reduceEvent(state, action.ev);
    case "reconcile":
      return reconcilePending(state, action.pending);
    case "reset":
      return initialStreamState;
  }
}

export type ConnState = "connecting" | "live" | "reconnecting";

export function useEventStream(sessionId: string | null) {
  const [state, dispatch] = useReducer(dispatchReduce, initialStreamState);
  const [conn, setConn] = useState<ConnState>("connecting");
  const retryRef = useRef(0);

  useEffect(() => {
    dispatch({ kind: "reset" });
    retryRef.current = 0;
    setConn("connecting");
    if (!sessionId) return;

    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;
    let cancelled = false;

    const reconcile = async () => {
      const res = await apiFetch<{ pending: PendingPermission[] }>(
        `/v1/sessions/${sessionId}/permissions`,
      );
      if (!cancelled && res) {
        dispatch({ kind: "reconcile", pending: res.pending });
      }
      // The replay history is bounded and a resumed session has none, so the
      // task list comes from the server's copy too.
      const todo = await apiFetch<{ items: TodoItem[] }>(`/v1/sessions/${sessionId}/todo`);
      if (!cancelled && todo && todo.items.length > 0) {
        dispatch({ kind: "event", ev: { method: "todo_update", params: { items: todo.items } } });
      }
    };

    const connect = () => {
      if (cancelled) return;
      // The server replays the session's history to every new listener, so
      // each (re)connect rebuilds state from scratch rather than appending.
      dispatch({ kind: "reset" });
      source = new EventSource(`${API_BASE}/v1/sessions/${sessionId}/events`);
      source.onopen = () => {
        retryRef.current = 0;
        setConn("live");
        void reconcile();
      };
      source.onmessage = (msg) => {
        try {
          const parsed = JSON.parse(msg.data) as {
            method?: string;
            params?: Record<string, unknown>;
          };
          if (parsed.method) {
            dispatch({
              kind: "event",
              ev: { method: parsed.method, params: parsed.params ?? {} },
            });
          }
        } catch {
          // Non-JSON keepalives are expected; ignore.
        }
      };
      source.onerror = () => {
        source?.close();
        if (cancelled) return;
        setConn("reconnecting");
        const delay = Math.min(1000 * 2 ** retryRef.current, 15000);
        retryRef.current += 1;
        retryTimer = setTimeout(connect, delay);
      };
    };

    connect();
    return () => {
      cancelled = true;
      source?.close();
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [sessionId]);

  // Belt-and-braces poll while a turn is in flight (3s).
  useEffect(() => {
    if (!sessionId || !state.turnInFlight) return;
    // Per-effect cancel: an in-flight fetch must not dispatch into the next
    // session after a switch (the reducer was just reset).
    let cancelled = false;
    const t = setInterval(async () => {
      const res = await apiFetch<{ pending: PendingPermission[] }>(
        `/v1/sessions/${sessionId}/permissions`,
      );
      if (!cancelled && res) dispatch({ kind: "reconcile", pending: res.pending });
    }, 3000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [sessionId, state.turnInFlight]);

  return { state, conn };
}
