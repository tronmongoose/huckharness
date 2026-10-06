// Theme choice: system (follow the OS), light, or dark. Stored per browser;
// storage can throw (private mode, blocked site data), so every access is
// guarded and the page still renders with the system theme.

export type ThemeChoice = "system" | "light" | "dark";

export const THEME_KEY = "bjorn.theme";
const ORDER: ThemeChoice[] = ["system", "light", "dark"];

/** The stored choice, or "system" when nothing valid is stored or storage throws. */
export function readTheme(): ThemeChoice {
  try {
    const v = window.localStorage.getItem(THEME_KEY);
    return v === "light" || v === "dark" ? v : "system";
  } catch {
    return "system";
  }
}

/** Sets or clears data-theme on <html>; "system" defers to the media query. */
export function applyTheme(choice: ThemeChoice): void {
  const root = document.documentElement;
  if (choice === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", choice);
}

/** Applies `choice` and remembers it when storage allows. */
export function saveTheme(choice: ThemeChoice): void {
  applyTheme(choice);
  try {
    if (choice === "system") window.localStorage.removeItem(THEME_KEY);
    else window.localStorage.setItem(THEME_KEY, choice);
  } catch {
    // Storage refused: the choice lasts for this page load only.
  }
}

/** The choice after `c` in the toggle's cycle. */
export function nextTheme(c: ThemeChoice): ThemeChoice {
  return ORDER[(ORDER.indexOf(c) + 1) % ORDER.length];
}
