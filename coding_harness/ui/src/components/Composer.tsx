// The prompt box. `@` opens a file picker, `/` at the start opens the command
// menu, Cmd/Ctrl+K opens the menu from anywhere. Enter sends, Shift+Enter is
// a newline, Cmd+Enter always sends, Esc closes a menu or stops the turn.
// The draft itself stays with Thread, which owns sending.

import { type RefObject, useEffect, useMemo, useState } from "react";

import { COMMAND_MENU_EVENT } from "@/hooks/useShortcuts";
import { apiFetch } from "@/lib/api";
import { type CommandRow, CommandMenu, filterCommands } from "./CommandMenu";
import { Mention, MentionChips } from "./Mention";
import { SkillSuggest } from "./SkillSuggest";

const MENTION_AT = /(?:^|\s)@([^\s@]*)$/;
// A command name being typed, or a skill name after `/skill `.
const SLASH_AT = /^\/(\S*|skill\s+\S*)$/;
const FILE_DEBOUNCE_MS = 60;

/** Text after the `@` being typed before the caret, or null. */
export function mentionQuery(before: string): string | null {
  const m = MENTION_AT.exec(before);
  return m ? m[1] : null;
}

/** Text after the leading `/` being typed before the caret, or null. */
export function slashQuery(before: string): string | null {
  const m = SLASH_AT.exec(before);
  return m ? m[1] : null;
}

function useFiles(query: string | null): string[] {
  const [files, setFiles] = useState<string[]>([]);
  useEffect(() => {
    if (query === null) return setFiles([]);
    let live = true;
    const t = setTimeout(async () => {
      const res = await apiFetch<{ files: string[] }>(`/v1/files?q=${encodeURIComponent(query)}`);
      if (live) setFiles(res?.files ?? []);
    }, FILE_DEBOUNCE_MS);
    return () => { live = false; clearTimeout(t); };
  }, [query]);
  return files;
}

function useCommands(wanted: boolean): CommandRow[] {
  const [rows, setRows] = useState<CommandRow[] | null>(null);
  useEffect(() => {
    if (!wanted || rows !== null) return;
    let live = true;
    void apiFetch<{ commands: CommandRow[] }>("/v1/commands").then((res) => {
      if (live) setRows(res?.commands ?? []);
    });
    return () => { live = false; };
  }, [wanted, rows]);
  return rows ?? [];
}

interface Props {
  draft: string;
  setDraft: (text: string) => void;
  onSend: () => void;
  onStop: () => void;
  busy: boolean;
  disabled: boolean;
  placeholder: string;
  inputRef: RefObject<HTMLTextAreaElement>;
}

// LONG-FN: one component holding the textarea, both menus and their keys.
export function Composer({ draft, setDraft, onSend, onStop, busy, disabled, placeholder, inputRef }: Props) {
  const [caret, setCaret] = useState(draft.length);
  const [forced, setForced] = useState(false);
  const [dismissed, setDismissed] = useState<string | null>(null);
  const [active, setActive] = useState(0);
  const [nextCaret, setNextCaret] = useState<number | null>(null);

  const before = draft.slice(0, Math.min(caret, draft.length));
  const open = dismissed !== draft;
  const mq = open ? mentionQuery(before) : null;
  const sq = open ? (forced ? "" : slashQuery(before)) : null;
  const files = useFiles(mq);
  // Paths the server listed are ones it resolved under cwd; only those get chips.
  const [known, setKnown] = useState<ReadonlySet<string>>(() => new Set());
  useEffect(() => {
    if (files.length > 0) setKnown((k) => new Set([...k, ...files]));
  }, [files]);
  const allCommands = useCommands(sq !== null);
  const commands = useMemo(() => (sq === null ? [] : filterCommands(allCommands, sq)), [allCommands, sq]);
  const mode = mq !== null && files.length > 0 ? "mention" : sq !== null && commands.length > 0 ? "command" : null;
  const count = mode === "mention" ? files.length : mode === "command" ? commands.length : 0;

  useEffect(() => setActive(0), [mq, sq]);
  useEffect(() => { if (active >= count && count > 0) setActive(0); }, [active, count]);

  useEffect(() => {
    const onMenu = () => {
      setForced(true);
      setDismissed(null);
      inputRef.current?.focus();
    };
    window.addEventListener(COMMAND_MENU_EVENT, onMenu);
    return () => window.removeEventListener(COMMAND_MENU_EVENT, onMenu);
  }, [inputRef]);

  useEffect(() => {
    if (nextCaret === null) return;
    inputRef.current?.setSelectionRange(nextCaret, nextCaret);
    setCaret(nextCaret);
    setNextCaret(null);
  }, [nextCaret, inputRef]);

  const replaceBefore = (text: string) => {
    setDraft(text + draft.slice(before.length));
    setNextCaret(text.length);
    setForced(false);
  };
  const pickFile = (path: string) => replaceBefore(before.replace(/@[^\s@]*$/, `@${path} `));
  const pickCommand = (row: CommandRow) => {
    if (forced && !draft.startsWith("/")) {
      const text = `${row.name} ${draft}`;
      setDraft(text);
      setNextCaret(text.length);
      setForced(false);
    } else {
      replaceBefore(`${row.name} `);
    }
  };
  const removeMention = (path: string) => {
    setDraft(draft.split(`@${path}`).join("").replace(/ {2,}/g, " "));
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return;
    const mod = e.metaKey || e.ctrlKey;
    if (e.key === "Enter" && mod) {
      e.preventDefault();
      return onSend();
    }
    if (mode && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      setActive((a) => (a + (e.key === "ArrowDown" ? 1 : count - 1)) % count);
      return;
    }
    if (mode && (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey))) {
      const row = mode === "command" ? commands[active] : null;
      // A complete command name runs on Enter instead of being re-inserted.
      if (!(e.key === "Enter" && row && row.name === draft.trim())) {
        e.preventDefault();
        return mode === "mention" ? pickFile(files[active]) : pickCommand(commands[active]);
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      return onSend();
    }
    if (e.key === "Escape") {
      if (mode || forced) {
        e.preventDefault();
        setDismissed(draft);
        setForced(false);
      } else if (busy) {
        e.preventDefault();
        onStop();
      }
    }
  };

  return (
    <div className="relative flex-1 min-w-0">
      <SkillSuggest draft={draft} onPick={(name) => {
        const text = `/skill ${name} ${draft}`;
        setDraft(text);
        setNextCaret(text.length);
      }} />
      <MentionChips draft={draft} known={known} onRemove={removeMention} />
      {mode === "mention" && <Mention files={files} active={active} onPick={pickFile} />}
      {mode === "command" && <CommandMenu rows={commands} active={active} onPick={pickCommand} />}
      <textarea
        ref={inputRef}
        value={draft}
        onChange={(e) => {
          setDraft(e.target.value);
          setCaret(e.target.selectionStart ?? e.target.value.length);
          setForced(false);
        }}
        onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? draft.length)}
        onKeyDown={onKeyDown}
        placeholder={placeholder}
        disabled={disabled}
        aria-autocomplete="list"
        aria-expanded={mode !== null}
        rows={Math.min(10, Math.max(2, draft.split("\n").length))}
        className="input font-serif text-sm leading-relaxed resize-none block"
      />
    </div>
  );
}
