// Agent prose as GitHub-flavored markdown. react-markdown renders no raw HTML,
// so model output cannot inject markup into the page.

import { isValidElement, memo, type ReactNode, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { highlightHtml, langOf } from "@/lib/highlight";

function CopyButton({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setDone(true);
      setTimeout(() => setDone(false), 1500);
    } catch {
      // Clipboard needs a secure context; over plain http it is refused.
    }
  };
  return (
    <button type="button" onClick={() => void copy()} aria-label="copy code"
      className="absolute top-1.5 right-1.5 label px-1.5 py-0.5 rounded bg-card/90 border border-rule opacity-70 hover:opacity-100 hover:text-accent">
      {done ? "copied" : "copy"}
    </button>
  );
}

// Memoized on (code, lang): a finished block keeps its DOM node and its
// highlight while later streaming deltas re-render the prose around it.
export const CodeBlock = memo(function CodeBlock({ code, lang }: { code: string; lang: string | null }) {
  return (
    <div className="relative my-3 group">
      <pre className="bg-card border border-rule rounded p-3 pr-14 overflow-x-auto font-mono text-[0.75rem] leading-relaxed">
        <code className={`hljs${lang ? ` language-${lang}` : ""}`}
          dangerouslySetInnerHTML={{ __html: highlightHtml(code, lang) }} />
      </pre>
      <CopyButton text={code} />
    </div>
  );
});

const components: Components = {
  a: ({ children, href }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline underline-offset-2"
    >
      {children}
    </a>
  ),
  pre: ({ children }) => {
    // react-markdown hands <pre> one <code> child carrying the fence language.
    const code = isValidElement<{ className?: string; children?: ReactNode }>(children)
      ? children.props : { className: undefined, children };
    return <CodeBlock code={String(code.children ?? "").replace(/\n$/, "")}
      lang={langOf(code.className)} />;
  },
  code: ({ children }) => (
    <code className="font-mono text-[0.8em] bg-card border border-rule/60 rounded px-1 py-0.5">
      {children}
    </code>
  ),
  ul: ({ children }) => <ul className="list-disc pl-6 my-2 space-y-1">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-6 my-2 space-y-1">{children}</ol>,
  h1: ({ children }) => <h3 className="font-serif text-xl mt-4 mb-2">{children}</h3>,
  h2: ({ children }) => <h4 className="font-serif text-lg mt-4 mb-2">{children}</h4>,
  h3: ({ children }) => <h5 className="font-serif text-base font-semibold mt-3 mb-1">{children}</h5>,
  p: ({ children }) => <p className="my-2">{children}</p>,
  blockquote: ({ children }) => (
    <blockquote className="border-l-2 border-rule pl-3 text-muted my-2">{children}</blockquote>
  ),
  table: ({ children }) => (
    <div className="overflow-x-auto my-3">
      <table className="text-sm border-collapse">{children}</table>
    </div>
  ),
  th: ({ children }) => (
    <th className="border-b border-rule px-2 py-1 text-left font-mono text-[0.7rem] uppercase tracking-wider text-muted">
      {children}
    </th>
  ),
  td: ({ children }) => <td className="border-b border-rule/60 px-2 py-1 align-top">{children}</td>,
};

export const Markdown = memo(function Markdown({ text }: { text: string }) {
  return (
    <div className="text-base leading-relaxed break-words">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
});
