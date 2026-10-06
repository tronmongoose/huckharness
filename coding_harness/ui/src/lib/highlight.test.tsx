import { render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Markdown } from "@/components/Markdown";
import { highlightHtml, hljs, knownLanguage, langOf } from "./highlight";

afterEach(() => vi.restoreAllMocks());

describe("highlight", () => {
  it("knows the registered grammars and their aliases", () => {
    for (const l of ["python", "typescript", "tsx", "javascript", "json", "bash", "sh",
      "yaml", "toml", "markdown", "diff"]) expect(knownLanguage(l)).toBe(true);
    expect(knownLanguage("cobol")).toBe(false);
    expect(langOf("language-TSX")).toBe("tsx");
    expect(langOf(undefined)).toBeNull();
  });

  it("escapes unknown languages instead of highlighting them", () => {
    expect(highlightHtml("<b>&</b>", "cobol")).toBe("&lt;b&gt;&amp;&lt;/b&gt;");
    expect(highlightHtml("def f(): pass", "python")).toContain("hljs-keyword");
  });

  it("highlights the same content once", () => {
    const spy = vi.spyOn(hljs, "highlight");
    const code = `x = ${Math.random()}`;
    highlightHtml(code, "python");
    highlightHtml(code, "python");
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it("a finished block keeps its node while streaming text grows", () => {
    const spy = vi.spyOn(hljs, "highlight");
    const block = "```python\nprint('memo-node')\n```\n\n";
    const { container, rerender } = render(<Markdown text={`${block}more`} />);
    const first = container.querySelector("pre code");
    rerender(<Markdown text={`${block}more text arriving`} />);
    rerender(<Markdown text={`${block}more text arriving still`} />);
    expect(container.querySelector("pre code")).toBe(first);
    expect(spy).toHaveBeenCalledTimes(1);
    expect(first?.textContent).toBe("print('memo-node')");
  });
});
