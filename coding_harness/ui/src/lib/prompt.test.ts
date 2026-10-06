import { describe, expect, it } from "vitest";

import { routeWords, showPrompt } from "./prompt";

describe("showPrompt", () => {
  it("shows a skill prompt as the skill and its task", () => {
    expect(showPrompt("[skill: design-tenets]\n---\nname: x\n---\nbody\n\n---\n\nTask: restyle it"))
      .toEqual({ skill: "design-tenets", text: "restyle it", notes: [] });
  });

  it("leaves an ordinary prompt alone", () => {
    expect(showPrompt("fix the test")).toEqual({ skill: null, text: "fix the test", notes: [] });
  });

  it("splits attached notes off the prompt", () => {
    const p = "what is this?\n\n---\n\nAttached notes:\n\nNote finance/budget.md (confidential):\n```markdown\nx\n```";
    expect(showPrompt(p)).toEqual({ skill: null, text: "what is this?", notes: ["finance/budget.md"] });
  });
});

describe("routeWords", () => {
  it("says who picked the model", () => {
    expect(routeWords("override_explicit_model")).toBe("pinned");
    expect(routeWords("complexity_low")).toBe("router");
  });
});
