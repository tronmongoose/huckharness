// Editorial primitives (pattern forked from nemoclaw-smb str/shared.tsx,
// retuned to the design-tenets tokens). Whitespace + hairline rules, not boxes.

import type { ReactNode } from "react";

export function SectionLabel({ children }: { children: ReactNode }) {
  return <div className="label mb-2">{children}</div>;
}

export function Rule() {
  return <hr className="rule my-6" />;
}

export function KV({ k, v }: { k: string; v: ReactNode }) {
  return (
    <div className="flex items-baseline gap-3 py-0.5">
      <span className="label w-28 shrink-0">{k}</span>
      <span className="font-mono text-xs text-ink/90 break-all">{v}</span>
    </div>
  );
}

// Specification table (TX-02 datasheet convention): KV rows framed by
// hairline top/bottom rules with per-row dividers — a component datasheet.
export function SpecTable({ children }: { children: ReactNode }) {
  return (
    <div className="border-y border-rule divide-y divide-rule/60 [&>div]:py-1.5">
      {children}
    </div>
  );
}

export function EmptyState({ children }: { children: ReactNode }) {
  return (
    <p className="text-sm italic text-muted py-4">{children}</p>
  );
}

// shadcn-idiom button (round-2 restyle). Variants map to the component
// classes in index.css. TextAction below stays for quiet inline actions.
export function Button({
  onClick,
  children,
  variant = "solid",
  disabled,
  title,
}: {
  onClick: () => void;
  children: ReactNode;
  variant?: "solid" | "outline" | "danger";
  disabled?: boolean;
  title?: string;
}) {
  const cls =
    variant === "outline"
      ? "btn-outline"
      : variant === "danger"
        ? "btn-danger"
        : "btn";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={cls}
    >
      {children}
    </button>
  );
}

// Quiet text control — tenet 8: not a button group.
export function TextAction({
  onClick,
  children,
  tone = "default",
  disabled,
  title,
}: {
  onClick: () => void;
  children: ReactNode;
  tone?: "default" | "accent" | "danger";
  disabled?: boolean;
  title?: string;
}) {
  const color =
    tone === "accent"
      ? "text-accent hover:text-accent/80"
      : tone === "danger"
        ? "text-muted hover:text-danger"
        : "text-muted hover:text-ink";
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`font-mono text-[0.7rem] uppercase tracking-[0.14em] underline underline-offset-4 decoration-rule ${color} disabled:opacity-40 disabled:no-underline`}
    >
      {children}
    </button>
  );
}
