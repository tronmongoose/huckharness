import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { FileChange } from "@/lib/types";
import { DiffView, lineClass } from "./DiffView";

const file: FileChange = {
  path: "src/a.py",
  status: "modified",
  diff: "--- a/src/a.py\n+++ b/src/a.py\n@@ -1,2 +1,2 @@\n keep\n-old\n+new",
  truncated: false,
};

describe("DiffView", () => {
  it("classes each line by its first character", () => {
    expect(lineClass("@@ -1 +1 @@")).toContain("diff-hunk");
    expect(lineClass("+++ b/x")).toContain("diff-meta");
    expect(lineClass("--- a/x")).toContain("diff-meta");
    expect(lineClass("+new")).toContain("diff-add");
    expect(lineClass("-old")).toContain("diff-del");
    expect(lineClass(" same")).toContain("diff-ctx");
  });

  it("renders path, status and toned lines", () => {
    render(<DiffView file={file} />);
    expect(screen.getByText("src/a.py")).toBeInTheDocument();
    expect(screen.getByText("modified")).toBeInTheDocument();
    expect(screen.getByText("+new").className).toContain("diff-add");
    expect(screen.getByText("-old").className).toContain("diff-del");
  });

  it("rejects only after the inline confirm", () => {
    const onReject = vi.fn();
    render(<DiffView file={file} onReject={onReject} onAccept={() => {}} />);
    fireEvent.click(screen.getByText("reject"));
    expect(onReject).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("confirm reject"));
    expect(onReject).toHaveBeenCalledOnce();
  });

  it("accept is a local mark and hides the controls", () => {
    const onAccept = vi.fn();
    const { rerender } = render(<DiffView file={file} onAccept={onAccept} onReject={() => {}} />);
    fireEvent.click(screen.getByText("accept"));
    expect(onAccept).toHaveBeenCalledOnce();
    rerender(<DiffView file={file} accepted onAccept={onAccept} onReject={() => {}} />);
    expect(screen.getByText("accepted")).toBeInTheDocument();
    expect(screen.queryByText("reject")).toBeNull();
  });
});
