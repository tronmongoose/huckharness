import { describe, expect, it } from "vitest";

import { LOADING, THINKING, phaseWord, pick } from "./thinkingWords";
import type { Phase } from "./types";

const ph = (kind: Phase["kind"], detail = ""): Phase => ({ kind, since: 0, detail, tokens: 0 });

describe("thinking words", () => {
  it("picks deterministically and moves with the tick", () => {
    expect(pick(THINKING, 3, 0)).toBe(pick(THINKING, 3, 0));
    expect(pick(THINKING, 3, 1)).not.toBe(pick(THINKING, 3, 0));
  });

  it("names the tool and its argument", () => {
    expect(phaseWord(ph("tool", "Read"), 1, 0, { summary: "core/done_gate.py" }))
      .toBe("Reading core/done_gate.py");
    expect(phaseWord(ph("tool", "Grep"), 1, 0, { summary: "baseline" })).toBe("Searching for baseline");
    expect(phaseWord(ph("tool", "Explore"), 1, 0, { summary: "where?" })).toBe("Dispatching a scout");
    expect(phaseWord(ph("tool", "Mystery"), 1, 0)).toBe("Running Mystery");
  });

  it("says loading when the model is not resident, and is literal for gates", () => {
    expect(LOADING).toContain(phaseWord(ph("waiting", "m"), 1, 0, { loading: true }));
    expect(THINKING).toContain(phaseWord(ph("waiting", "m"), 1, 0));
    expect(phaseWord(ph("baseline"), 1, 0)).toBe("Running the test baseline");
    expect(phaseWord(ph("approval"), 1, 5)).toBe("Waiting on you");
  });
});
