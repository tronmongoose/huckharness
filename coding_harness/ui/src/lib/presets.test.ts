import { describe, expect, it } from "vitest";

import { ENVELOPE_PRESETS, presetByKey } from "./presets";

describe("envelope presets", () => {
  it("read-only preset grants only read access", () => {
    const p = presetByKey("read-only-repo");
    expect(p.envelope).not.toBeNull();
    for (const g of p.envelope!.grants) {
      expect(g.access).toBe("read");
    }
  });

  it("scratchpad preset confines writes to /tmp", () => {
    const p = presetByKey("scratchpad");
    const writes = p.envelope!.grants.filter((g) => g.access === "write");
    expect(writes.length).toBeGreaterThan(0);
    for (const g of writes) {
      expect(String(g.path_glob)).toMatch(/^\/tmp\//);
    }
  });

  it("project preset sends no spec, so the server applies its cwd preset", () => {
    expect(presetByKey("project").envelope).toBeNull();
  });

  it("unknown key falls back to the most restrictive preset", () => {
    expect(presetByKey("nope").key).toBe(ENVELOPE_PRESETS[0].key);
    expect(ENVELOPE_PRESETS[0].key).toBe("read-only-repo");
  });
});
