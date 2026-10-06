// Syntax highlighting for fenced code and tool diffs. highlight.js core with
// only the grammars the thread actually shows, so the bundle stays small.
// Output is HTML that highlight.js escaped itself, safe for innerHTML.

import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import diff from "highlight.js/lib/languages/diff";
import ini from "highlight.js/lib/languages/ini";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import markdown from "highlight.js/lib/languages/markdown";
import python from "highlight.js/lib/languages/python";
import typescript from "highlight.js/lib/languages/typescript";
import yaml from "highlight.js/lib/languages/yaml";

hljs.registerLanguage("python", python);
hljs.registerLanguage("typescript", typescript);
hljs.registerLanguage("javascript", javascript);
hljs.registerLanguage("json", json);
hljs.registerLanguage("bash", bash);
hljs.registerLanguage("yaml", yaml);
hljs.registerLanguage("ini", ini);
hljs.registerLanguage("markdown", markdown);
hljs.registerLanguage("diff", diff);
// tsx/jsx: the TS and JS grammars tolerate JSX well enough for a reader.
hljs.registerAliases(["tsx", "ts", "mts"], { languageName: "typescript" });
hljs.registerAliases(["jsx", "js", "mjs", "cjs"], { languageName: "javascript" });
hljs.registerAliases(["toml"], { languageName: "ini" });
hljs.registerAliases(["sh", "shell", "zsh", "console"], { languageName: "bash" });

export { hljs };

// Finished blocks repeat on every streaming delta; highlight each once.
const CACHE_MAX = 300;
const cache = new Map<string, string>();

function escapeHtml(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Language id from a react-markdown className like "language-tsx", or null. */
export function langOf(className: string | undefined): string | null {
  const m = /language-([\w+-]+)/.exec(className ?? "");
  return m ? m[1].toLowerCase() : null;
}

/** True when a grammar (or alias) is registered for `lang`. */
export function knownLanguage(lang: string | null): boolean {
  return lang !== null && hljs.getLanguage(lang) !== undefined;
}

/** Highlighted HTML for `code`; escaped plain text for unknown languages. */
export function highlightHtml(code: string, lang: string | null): string {
  const key = `${lang ?? ""}\u0000${code}`;
  const hit = cache.get(key);
  if (hit !== undefined) return hit;
  const html = knownLanguage(lang)
    ? hljs.highlight(code, { language: lang as string, ignoreIllegals: true }).value
    : escapeHtml(code);
  if (cache.size >= CACHE_MAX) cache.delete(cache.keys().next().value as string);
  cache.set(key, html);
  return html;
}
