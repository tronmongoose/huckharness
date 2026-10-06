import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import { SUGGEST_DEBOUNCE_MS, SkillSuggest } from "./SkillSuggest";

const SUGGESTIONS = { suggestions: [
  { name: "code-review", description: "Review a diff", score: 5 },
  { name: "dataviz", description: "Charts", score: 2 },
] };

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(SUGGEST_DEBOUNCE_MS + 10);
  });
}

describe("SkillSuggest", () => {
  it("debounces: one request after typing stops", async () => {
    routeFetch({ "/v1/skills/suggest": SUGGESTIONS });
    const { rerender } = render(<SkillSuggest draft="review my diff" onPick={vi.fn()} />);
    rerender(<SkillSuggest draft="review my diff please" onPick={vi.fn()} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(SUGGEST_DEBOUNCE_MS - 50); });
    expect(fetch).not.toHaveBeenCalled();
    await settle();
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toContain("q=review%20my%20diff%20please");
    expect(screen.getByText("/skill code-review")).toBeInTheDocument();
  });

  it("inserts the skill on click", async () => {
    routeFetch({ "/v1/skills/suggest": SUGGESTIONS });
    const onPick = vi.fn();
    render(<SkillSuggest draft="review my diff please" onPick={onPick} />);
    await settle();
    fireEvent.click(screen.getByText("/skill dataviz"));
    expect(onPick).toHaveBeenCalledWith("dataviz");
  });

  it("stays quiet for short drafts and slash commands", async () => {
    routeFetch({ "/v1/skills/suggest": SUGGESTIONS });
    const { rerender } = render(<SkillSuggest draft="short" onPick={vi.fn()} />);
    await settle();
    rerender(<SkillSuggest draft="/skill code-review do it" onPick={vi.fn()} />);
    await settle();
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.queryByLabelText("suggested skills")).toBeNull();
  });
});
