import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { applyTheme, nextTheme, readTheme, saveTheme, THEME_KEY } from "./theme";

// An in-memory Storage: newer Node ships its own global localStorage that
// shadows jsdom's and has no methods unless started with a storage file.
function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() { return m.size; },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => { m.delete(k); },
    setItem: (k, v) => { m.set(k, String(v)); },
  };
}

beforeEach(() => vi.stubGlobal("localStorage", memoryStorage()));

afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.removeAttribute("data-theme");
});

describe("theme", () => {
  it("persists a choice and reads it back", () => {
    saveTheme("dark");
    expect(window.localStorage.getItem(THEME_KEY)).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(readTheme()).toBe("dark");
    saveTheme("system");
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(readTheme()).toBe("system");
  });

  it("ignores junk in storage", () => {
    window.localStorage.setItem(THEME_KEY, "sepia");
    expect(readTheme()).toBe("system");
  });

  it("survives a localStorage that throws", () => {
    const boom = () => { throw new Error("SecurityError"); };
    vi.stubGlobal("localStorage", { getItem: boom, setItem: boom, removeItem: boom });
    expect(readTheme()).toBe("system");
    expect(() => saveTheme("light")).not.toThrow();
    expect(document.documentElement.dataset.theme).toBe("light");
  });

  it("survives a localStorage accessor that throws", () => {
    const spy = vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readTheme()).toBe("system");
    expect(() => saveTheme("dark")).not.toThrow();
    spy.mockRestore();
  });

  it("cycles system, light, dark", () => {
    expect(nextTheme("system")).toBe("light");
    expect(nextTheme("light")).toBe("dark");
    expect(nextTheme("dark")).toBe("system");
    applyTheme("light");
    expect(document.documentElement.dataset.theme).toBe("light");
  });
});
