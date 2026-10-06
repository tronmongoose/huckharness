import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ContextMeter } from "./ContextMeter";

describe("ContextMeter", () => {
  it("shows use against the window", () => {
    render(<ContextMeter context={{ used: 11200, max: 32000 }} />);
    expect(screen.getByText("11.2k / 32.0k (35%)")).toHaveClass("text-muted");
  });

  it("turns accent and points at /compact past 80%", () => {
    render(<ContextMeter context={{ used: 27000, max: 32000 }} />);
    const text = screen.getByText("27.0k / 32.0k (84%)");
    expect(text).toHaveClass("text-accent");
    expect(text.parentElement).toHaveAttribute("title", expect.stringContaining("/compact"));
  });

  it("renders nothing before the first turn", () => {
    const { container } = render(<ContextMeter context={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
