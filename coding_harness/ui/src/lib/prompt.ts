// Reading a turn prompt back: a skill prompt (serve_gui.SKILL_MARK) shows as
// the skill's name and the task, not the whole SKILL.md.

const SKILL_MARK = "[skill: ";
const TASK_SEP = "\n\n---\n\nTask: ";
// serve_gui.ATTACH_MARK: notes attached to a turn follow the prompt.
const ATTACH_MARK = "\n\n---\n\nAttached notes:\n\n";
const NOTE_HEAD = /^Note (\S+) \(/gm;

export interface ShownPrompt {
  skill: string | null;
  text: string;
  notes: string[];
}

export function showPrompt(prompt: string): ShownPrompt {
  const cut = prompt.indexOf(ATTACH_MARK);
  const body = cut >= 0 ? prompt.slice(0, cut) : prompt;
  const notes = cut >= 0
    ? [...prompt.slice(cut + ATTACH_MARK.length).matchAll(NOTE_HEAD)].map((m) => m[1])
    : [];
  if (!body.startsWith(SKILL_MARK)) return { skill: null, text: body, notes };
  const skill = body.slice(SKILL_MARK.length).split("]", 1)[0];
  const at = body.indexOf(TASK_SEP);
  return { skill, text: at >= 0 ? body.slice(at + TASK_SEP.length) : "", notes };
}

// Sensitivity tier in words, and whether only local models may see it.
export const TIER_NAMES = ["public", "internal", "confidential", "restricted"];
export const LOCAL_ONLY_TIER = 2;

// route_reason in words: who chose the model for this turn.
export function routeWords(reason: string | undefined): string {
  if (!reason) return "";
  if (reason === "override_explicit_model") return "pinned";
  if (reason.startsWith("override_turn")) return "pinned for this turn";
  if (reason === "override_force_local") return "local only";
  if (reason.startsWith("complexity") || reason.startsWith("sensitivity")) return "router";
  return reason.replace(/_/g, " ");
}
