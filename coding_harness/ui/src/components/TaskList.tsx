// The model's TodoWrite checklist, live off the event stream. A mono ledger:
// a mark per row, the running step in accent, finished steps struck quiet.

import type { TodoItem } from "@/lib/types";

const MARK: Record<TodoItem["status"], string> = {
  done: "[x]",
  in_progress: "[>]",
  pending: "[ ]",
};

export function taskSummary(items: TodoItem[]): string {
  const done = items.filter((t) => t.status === "done").length;
  return `${done} of ${items.length} done`;
}

export function TaskList({ items }: { items: TodoItem[] }) {
  if (items.length === 0) return null;
  return (
    <div className="py-2" aria-label="task list">
      <div className="label mb-1">tasks · {taskSummary(items)}</div>
      <ul className="space-y-0.5">
        {items.map((t) => (
          <li
            key={t.id}
            aria-current={t.status === "in_progress" ? "step" : undefined}
            className={`flex gap-2 font-mono text-[0.7rem] ${
              t.status === "in_progress"
                ? "text-accent"
                : t.status === "done"
                  ? "text-muted line-through"
                  : "text-ink/90"
            }`}
          >
            <span className="shrink-0">{MARK[t.status] ?? "[ ]"}</span>
            <span className="min-w-0 break-words">{t.text}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
