import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import { SkillsView, skillBody } from "./SkillsView";

afterEach(() => vi.unstubAllGlobals());

const list = { skills: [
  { name: "design-tenets", description: "house rules", source: "claude" },
  { name: "weekly-review", description: "run the review", source: "bjorn" },
] };

describe("SkillsView", () => {
  it("reads a borrowed skill, offers use and copy but not edit", async () => {
    routeFetch({
      "/v1/skills/design-tenets": { ...list.skills[0], editable: false,
        text: "---\nname: design-tenets\n---\n\n# Tenets\nBe plain." },
      "/v1/skills": list,
    });
    const onUse = vi.fn();
    render(<SkillsView onUse={onUse} />);
    fireEvent.click(await screen.findByText("design-tenets"));
    expect(await screen.findByRole("heading", { name: "Tenets" })).toBeInTheDocument();
    expect(screen.queryByText("edit")).toBeNull();
    expect(screen.getByText("copy to bjorn")).toBeInTheDocument();
    fireEvent.click(screen.getByText("use in session"));
    expect(onUse).toHaveBeenCalledWith("design-tenets");
  });

  it("saves a new skill to the bjorn root", async () => {
    const posts = routeFetch({ "/v1/skills": list });
    render(<SkillsView onUse={vi.fn()} />);
    fireEvent.click(screen.getByText("new skill"));
    fireEvent.change(screen.getByLabelText("skill name"), { target: { value: "triage" } });
    fireEvent.change(screen.getByLabelText("skill description"), { target: { value: "sort the inbox" } });
    fireEvent.change(screen.getByLabelText("skill body"), { target: { value: "1. Read." } });
    fireEvent.click(screen.getByText("save"));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toEqual({ url: expect.stringContaining("/v1/skills/triage"),
      body: { description: "sort the inbox", body: "1. Read." } });
  });

  it("strips frontmatter for the editor", () => {
    expect(skillBody("---\nname: x\ndescription: y\n---\n\nBody")).toBe("Body");
  });
});
