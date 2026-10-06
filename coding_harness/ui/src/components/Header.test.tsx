import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import type { ModelInfo } from "@/lib/types";
import { BranchTag, capability } from "./Header";

const m = (over: Partial<ModelInfo>): ModelInfo => ({ id: "x", backend: "ollama", text_only: false, ...over });

describe("model capability label", () => {
  it("reads the probe verdict", () => {
    expect(capability(m({ agent: true }))).toBe("agent");
    expect(capability(m({ agent: false }))).toBe("chat only");
    expect(capability(m({ agent: null }))).toBe("untested");
    expect(capability(m({ agent: null, probing: true }))).toBe("probing…");
    expect(capability(m({ backend: "claude-cli", text_only: true }))).toBe("text only");
  });
});

describe("BranchTag", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("shows the branch name", async () => {
    routeFetch({ "/v1/git": { branch: "feat-x", detached: false, head: "abc" } });
    render(<BranchTag />);
    expect(await screen.findByLabelText("branch")).toHaveTextContent("feat-x");
    expect(screen.queryByText(/detached/)).toBeNull();
  });

  it("marks a detached HEAD with its short sha", async () => {
    routeFetch({ "/v1/git": { branch: null, detached: true, head: "0123456789abcdef" } });
    render(<BranchTag />);
    expect(await screen.findByLabelText("branch")).toHaveTextContent("01234567");
    expect(screen.getByText(/\(detached\)/)).toBeInTheDocument();
  });
});
