// One tool call in the thread: a one-line summary that expands to the full
// command, the diff for file edits, and the output the model saw.

import { useState } from "react";

import { highlightHtml } from "@/lib/highlight";
import type { ToolStep } from "@/lib/types";

function str(v: unknown): string {
  return typeof v === "string" ? v : v == null ? "" : JSON.stringify(v, null, 2);
}

// "N items: a done, b in progress, c pending" for a TodoWrite list.
function todoSummary(items: unknown): string {
  const list = Array.isArray(items) ? items : [];
  const count = (s: string) =>
    list.filter((it) => (it as { status?: unknown } | null)?.status === s).length;
  return `${list.length} items: ${count("done")} done, ${count("in_progress")} in progress, ${count("pending")} pending`;
}

// The one argument that identifies the call, per tool.
export function stepSummary(step: ToolStep): string {
  const a = step.args ?? {};
  switch (step.tool) {
    case "Bash":
      return str(a.command);
    case "Read":
    case "Write":
    case "Edit":
      return str(a.file_path ?? a.path);
    case "Grep":
    case "Glob":
      return [str(a.pattern), str(a.path)].filter(Boolean).join("  in  ");
    case "Explore":
      return str(a.task);
    case "TodoWrite":
      return todoSummary(a.items);
    default:
      return step.argsPreview;
  }
}

function status(step: ToolStep): { text: string; tone: string } {
  if (step.sentinelAllowed === false) return { text: "blocked", tone: "text-danger" };
  if (step.isError) return { text: "error", tone: "text-danger" };
  if (step.resultPreview !== undefined) return { text: "ok", tone: "text-muted" };
  return { text: "running", tone: "text-accent" };
}

// Edit and Write input as a unified-diff fragment, colored by the diff grammar.
function DiffLines({ removed, added }: { removed?: string; added: string }) {
  const sign = (text: string, mark: string) => text.split("\n").map((l) => `${mark} ${l}`);
  const lines = [...(removed === undefined ? [] : sign(removed, "-")), ...sign(added, "+")];
  return (
    <pre className="font-mono text-[0.72rem] leading-relaxed whitespace-pre-wrap [overflow-wrap:anywhere]">
      <code className="hljs language-diff"
        dangerouslySetInnerHTML={{ __html: highlightHtml(lines.join("\n"), "diff") }} />
    </pre>
  );
}

function Detail({ step }: { step: ToolStep }) {
  const a = step.args ?? {};
  let input = null;
  if (step.tool === "Edit") {
    input = <DiffLines removed={str(a.old_string)} added={str(a.new_string)} />;
  } else if (step.tool === "Write") {
    input = <DiffLines added={str(a.content)} />;
  } else if (step.tool !== "Bash" && step.tool !== "Read" && step.args) {
    input = <pre className="font-mono text-[0.72rem] whitespace-pre-wrap [overflow-wrap:anywhere]">{str(step.args)}</pre>;
  }
  return (
    <div className="ml-2 sm:ml-5 mt-1 mb-2 border-l border-rule pl-3 space-y-2 max-h-96 overflow-auto min-w-0">
      {step.tool === "Bash" && (
        <pre className="font-mono text-[0.72rem] whitespace-pre-wrap [overflow-wrap:anywhere] text-gold">$ {str(a.command)}</pre>
      )}
      {input}
      {step.sentinelAllowed === false && step.sentinelReason && (
        <p className="font-mono text-[0.72rem] text-danger">blocked: {step.sentinelReason}</p>
      )}
      {step.result !== undefined && step.tool !== "Write" && step.tool !== "Edit" && (
        <pre className="font-mono text-[0.72rem] whitespace-pre-wrap [overflow-wrap:anywhere] text-muted">
          {step.result || "(no output)"}
        </pre>
      )}
      {step.auditHash && (
        <p className="font-mono text-[0.6rem] text-muted">audit #{step.auditHash.slice(0, 12)}</p>
      )}
    </div>
  );
}

export function StepView({ step }: { step: ToolStep }) {
  const [open, setOpen] = useState(false);
  const st = status(step);
  return (
    <div className="my-1">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="w-full flex items-baseline gap-2 text-left py-0.5 hover:bg-card/60 rounded"
      >
        <span className="font-mono text-[0.65rem] text-muted w-3">{open ? "▾" : "▸"}</span>
        <span className="font-mono text-[0.72rem] text-ink shrink-0">{step.tool}</span>
        <span className="font-mono text-[0.72rem] text-muted truncate flex-1 min-w-0">
          {stepSummary(step)}
        </span>
        <span className={`font-mono text-[0.6rem] uppercase tracking-widest ${st.tone}`}>
          {st.text}
        </span>
      </button>
      {open && <Detail step={step} />}
    </div>
  );
}
