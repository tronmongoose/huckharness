import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import { BrainView } from "./BrainView";

afterEach(() => vi.unstubAllGlobals());

describe("BrainView pin", () => {
  it("offers pin only with a session and passes the note", async () => {
    routeFetch({
      "/v1/brain/status": { configured: true, backend: "mcp", ok: true, age_hours: null, stale: false, warnings: [] },
      "/v1/brain/search": { hits: [
        { path: "startup/plan.md", vault: "startup", sensitivity: "internal", tier: 1, score: 1, snippet: "plan" },
      ] },
      "/v1/brain/page": { path: "startup/plan.md", sensitivity: "internal", tier: 1, content: "# Plan" },
    });
    const onPin = vi.fn();
    render(<BrainView onAttach={vi.fn()} onPin={onPin} />);
    fireEvent.click(await screen.findByText("list"));
    fireEvent.change(await screen.findByLabelText("search the brain"), { target: { value: "plan" } });
    fireEvent.submit(screen.getByLabelText("search the brain").closest("form")!);
    fireEvent.click(await screen.findByText("startup/plan.md"));
    fireEvent.click(await screen.findByText("pin to session"));
    expect(onPin).toHaveBeenCalledWith({ path: "startup/plan.md", tier: 1 });
  });
});
