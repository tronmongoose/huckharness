import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { routeFetch } from "@/test/route-fetch";
import { SkillsList } from "./SkillsList";
import { SkillsView, skillBody } from "./SkillsView";

afterEach(() => vi.unstubAllGlobals());

const list = {
  skills: [
    { name: "design-tenets", description: "house rules", source: "claude", bucket: "Design & media" },
    { name: "artist", description: "draw things", source: "claude", bucket: "Design & media" },
    { name: "money", description: "the household ledger", source: "bjorn", bucket: "Money & household" },
    { name: "weekly-review", description: "run the review", source: "bjorn", bucket: "Other" },
  ],
  buckets: [
    { name: "Money & household", count: 1 },
    { name: "Briefs & digests", count: 0 },
    { name: "Design & media", count: 2 },
    { name: "Other", count: 1 },
  ],
};

const tenets = { ...list.skills[0], editable: false, category: "",
  text: "---\nname: design-tenets\n---\n\n# Tenets\nBe plain." };

describe("SkillsView", () => {
  it("shows one tile per non-empty bucket with its count and first names", async () => {
    routeFetch({ "/v1/skills": list });
    render(<SkillsView onUse={vi.fn()} />);
    const tile = await screen.findByRole("button", { name: "open Design & media" });
    expect(within(tile).getByTestId("tile-count")).toHaveTextContent("2");
    expect(tile).toHaveTextContent("design-tenets · artist");
    expect(screen.getAllByTestId("tile-count").map((n) => n.textContent)).toEqual(["1", "2", "1"]);
    expect(screen.queryByText("Briefs & digests")).toBeNull();
  });

  it("goes tile to list to detail and back by the breadcrumb", async () => {
    routeFetch({ "/v1/skills/design-tenets": tenets, "/v1/skills": list });
    const onUse = vi.fn();
    render(<SkillsView onUse={onUse} />);
    fireEvent.click(await screen.findByRole("button", { name: "open Design & media" }));
    expect(screen.getByText("artist")).toBeInTheDocument();
    expect(screen.queryByText("money")).toBeNull();
    fireEvent.click(screen.getByText("design-tenets"));
    expect(await screen.findByRole("heading", { name: "Tenets" })).toBeInTheDocument();
    expect(screen.queryByText("edit")).toBeNull();
    expect(screen.getByText("copy to bjorn")).toBeInTheDocument();
    fireEvent.click(screen.getByText("use in session"));
    expect(onUse).toHaveBeenCalledWith("design-tenets");
    const crumbs = screen.getByRole("navigation", { name: "skills breadcrumb" });
    fireEvent.click(within(crumbs).getByText("Design & media"));
    expect(screen.getByText("artist")).toBeInTheDocument();
    fireEvent.click(within(crumbs).getByText("skills"));
    expect(await screen.findByRole("button", { name: "open Money & household" })).toBeInTheDocument();
  });

  it("searches across every bucket and labels each hit with its bucket", async () => {
    routeFetch({ "/v1/skills": list });
    render(<SkillsView onUse={vi.fn()} />);
    await screen.findByRole("button", { name: "open Design & media" });
    fireEvent.change(screen.getByLabelText("search skills"), { target: { value: "r" } });
    expect(screen.getByText("4 matching skills")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("search skills"), { target: { value: "house" } });
    expect(screen.getByText("2 matching skills")).toBeInTheDocument();
    expect(screen.getByText("design-tenets")).toBeInTheDocument();
    expect(screen.getByText("money")).toBeInTheDocument();
    expect(screen.getByText("Money & household")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^open / })).toBeNull();
  });

  it("opens at the bucket it was given", async () => {
    routeFetch({ "/v1/skills": list });
    render(<SkillsView onUse={vi.fn()} initialBucket="Other" />);
    expect(await screen.findByText("weekly-review")).toBeInTheDocument();
    expect(screen.queryByText("artist")).toBeNull();
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

  it("files a new skill under a chosen bucket", async () => {
    const posts = routeFetch({ "/v1/skills": list });
    render(<SkillsView onUse={vi.fn()} />);
    await screen.findByRole("button", { name: "open Design & media" });
    fireEvent.click(screen.getByText("new skill"));
    const select = screen.getByLabelText("skill bucket");
    expect(within(select).queryByText("Other")).toBeNull();
    fireEvent.change(select, { target: { value: "Design & media" } });
    fireEvent.change(screen.getByLabelText("skill name"), { target: { value: "sketch" } });
    fireEvent.change(screen.getByLabelText("skill description"), { target: { value: "draw" } });
    fireEvent.click(screen.getByText("save"));
    await vi.waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0].body).toEqual({ description: "draw", body: "", category: "Design & media" });
  });

  it("strips frontmatter for the editor", () => {
    expect(skillBody("---\nname: x\ndescription: y\n---\n\nBody")).toBe("Body");
  });
});

describe("SkillsList", () => {
  it("collapses to bucket names with counts and opens the clicked bucket", async () => {
    routeFetch({ "/v1/skills": list });
    const onOpen = vi.fn();
    render(<SkillsList onOpen={onOpen} />);
    expect(await screen.findByText("Skills · 4")).toBeInTheDocument();
    expect(screen.queryByText("design-tenets")).toBeNull();
    expect(screen.queryByText("Briefs & digests")).toBeNull();
    const row = screen.getByRole("button", { name: /Design & media/ });
    expect(row).toHaveTextContent("2");
    fireEvent.click(row);
    expect(onOpen).toHaveBeenCalledWith("Design & media");
  });
});
