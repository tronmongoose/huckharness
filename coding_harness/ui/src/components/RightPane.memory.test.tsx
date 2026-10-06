import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import { RightPane } from "./RightPane";

afterEach(() => vi.unstubAllGlobals());

describe("RightPane memory tab", () => {
  it("badges the proposal count and shows the review and pins", async () => {
    routeFetch({ "/v1/sessions/s1/memories": { proposals: [], pinned: [] } });
    render(
      <RightPane sessionId="s1" envelope={null} revoked={false} pending={[]} trail={[]}
        proposals={[{ id: "1", name: "per-file-tests", description: "Run tests per file", type: "feedback" }]}
        pinned={[{ path: "startup/plan.md", tier: 1 }]} />,
    );
    expect(screen.getByLabelText("1 proposals")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: /memory/ }));
    expect(await screen.findByLabelText("proposal per-file-tests")).toBeInTheDocument();
    expect(screen.getByText("startup/plan.md")).toBeInTheDocument();
  });
});
