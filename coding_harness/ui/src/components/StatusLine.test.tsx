import { describe, expect, it } from "vitest";

import { phaseText, waitText } from "./StatusLine";

const ps = (models: Array<[string, number]>) => ({
  reachable: true,
  resident: models.map(([model, size_gb]) => ({ model, size_gb, until: null })),
});

describe("status line text", () => {
  it("names what holds memory when the model is not loaded", () => {
    expect(waitText("phi4-mini", ps([["gpt-oss:20b", 13.1], ["gemma4:26b", 19]]))).toBe(
      "waiting for phi4-mini · not loaded, resident: gpt-oss:20b 13.1 GB, gemma4:26b 19 GB",
    );
  });

  it("calls a loaded model's wait a queue, not a load", () => {
    expect(waitText("gpt-oss:20b", ps([["gpt-oss:20b", 13.1]]))).toContain("queued");
  });

  it("says when Ollama itself is down", () => {
    expect(waitText("m", { reachable: false, resident: [] })).toContain("not answering");
  });

  it("shows the baseline command and a generation rate", () => {
    expect(phaseText({ kind: "baseline", since: 0, detail: "make test", tokens: 0 }, 0, null))
      .toBe("running baseline: make test");
    expect(phaseText({ kind: "generating", since: 0, detail: "m", tokens: 40 }, 2000, null))
      .toBe("generating on m · 20 tok/s");
  });
});
