// The autonomy ladder as the GUI names it. The ladder subsumes mode on the
// server: OFF is Plan, every other level is Act (core/mode.mode_for). Offering
// both controls would let one silently override the other, so every picker
// offers the ladder and names the mode it implies.

export const AUTONOMY_LEVELS = [
  { key: "off", implies: "plan", hint: "read-only" },
  { key: "low", implies: "act", hint: "edits under cwd, tests, lint" },
  { key: "medium", implies: "act", hint: "adds commits, moves, installs" },
  { key: "high", implies: "act", hint: "adds network" },
] as const;

export type AutonomyKey = (typeof AUTONOMY_LEVELS)[number]["key"];

// Unknown keys fall back to LOW, the server's default level.
export function levelByKey(key: string | null | undefined) {
  return AUTONOMY_LEVELS.find((l) => l.key === key) ?? AUTONOMY_LEVELS[1];
}

// The levels that may run a plan: everything but read-only OFF.
export const ACT_LEVELS = AUTONOMY_LEVELS.filter((l) => l.key !== "off");
