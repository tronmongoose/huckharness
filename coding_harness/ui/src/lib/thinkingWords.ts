// The words under a running turn: instrument verbs while the model thinks,
// and plain descriptions of the tool or wait it is actually on.

import type { Phase } from "./types";

export const THINKING = [
  "Calibrating", "Triangulating", "Cross-referencing", "Plotting a course",
  "Annealing", "Reconciling", "Indexing", "Collating", "Interpolating", "Surveying",
];
export const LOADING = ["Warming the model", "Loading weights", "Spinning up"];

const TOOL_VERBS: Record<string, string> = {
  Read: "Reading", Grep: "Searching for", Glob: "Surveying", Bash: "Running",
  Edit: "Machining", Write: "Drafting", Brain: "Consulting the archive",
  Explore: "Dispatching a scout", TodoWrite: "Updating the plan",
};

// Deterministic: the same turn and tick always give the same word, and
// neighbouring turns start at different points in the list.
export function pick(list: string[], turn: number, tick: number): string {
  return list[((turn * 7 + tick) % list.length + list.length) % list.length];
}

// What the thinking block says for a phase. `summary` is the running tool's
// argument (path, pattern, command); `loading` means the model isn't resident.
export function phaseWord(phase: Phase, turn: number, tick: number,
  opts: { summary?: string; loading?: boolean } = {}): string {
  switch (phase.kind) {
    case "baseline":
      return "Running the test baseline";
    case "approval":
      return "Waiting on you";
    case "tool": {
      const verb = TOOL_VERBS[phase.detail] ?? `Running ${phase.detail}`;
      const noArg = phase.detail === "Brain" || phase.detail === "Explore" || phase.detail === "TodoWrite";
      return opts.summary && !noArg ? `${verb} ${opts.summary}` : verb;
    }
    case "waiting":
      return pick(opts.loading ? LOADING : THINKING, turn, tick);
    case "generating":
      return "Composing";
    default:
      return pick(THINKING, turn, tick);
  }
}
