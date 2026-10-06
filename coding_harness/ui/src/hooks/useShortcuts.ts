// Global keyboard shortcuts. Cmd on macOS, Ctrl elsewhere; either works.
// Cmd+Enter (send) and Esc (stop) live on the composer's own keydown, since
// they act on the focused draft; they are listed here for the help sheet.

import { useEffect, useRef } from "react";

export const SHORTCUTS: Array<{ keys: string; does: string }> = [
  { keys: "⌘K", does: "open the command menu" },
  { keys: "⌘↵", does: "send, even with a menu open" },
  { keys: "⌘/", does: "toggle the session rail" },
  { keys: "⌘1–9", does: "switch to the nth live session" },
  { keys: "Esc", does: "close a menu, else stop the turn" },
  { keys: "@", does: "attach a file by path" },
  { keys: "/", does: "slash commands at line start" },
];

// Composer listens for this; the menu lives inside the composer.
export const COMMAND_MENU_EVENT = "bjorn:command-menu";

export interface ShortcutHandlers {
  toggleRail: () => void;
  session: (n: number) => void;
}

/** Routes one keydown to a handler; true when it was a shortcut. */
export function dispatchShortcut(e: KeyboardEvent, h: ShortcutHandlers): boolean {
  if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return false;
  if (e.key === "k" || e.key === "K") {
    window.dispatchEvent(new Event(COMMAND_MENU_EVENT));
    return true;
  }
  if (e.key === "/") {
    h.toggleRail();
    return true;
  }
  if (/^[1-9]$/.test(e.key)) {
    h.session(Number(e.key));
    return true;
  }
  return false;
}

/** Installs the global shortcuts for the life of the calling component. */
export function useShortcuts(handlers: ShortcutHandlers): void {
  const ref = useRef(handlers);
  ref.current = handlers;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (dispatchShortcut(e, ref.current)) e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
}
