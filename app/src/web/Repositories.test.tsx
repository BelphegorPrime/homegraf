import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RepoFactoryStatus, RepoInfo } from "../shared/types";
import { factoryState, RepositoriesView } from "./Repositories";

const ready: RepoFactoryStatus = { webhook: true, labels: true, agentAccess: true, allowed: true };

const repo = (over: Partial<RepoInfo> = {}): RepoInfo => ({
  name: "owner/repository",
  private: true,
  default_branch: "main",
  updated: "2026-03-04T05:06:07Z",
  url: "http://forgejo.lab.example/owner/repository",
  factory: ready,
  ...over,
});

const render = (props: Parameters<typeof RepositoriesView>[0]) => renderToStaticMarkup(<RepositoriesView {...props} />);

/** React escapes entities in server markup; compare against the text a browser shows. */
const text = (html: string) =>
  html
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&");

describe("factoryState", () => {
  it("is ready when every check is true", () => {
    expect(factoryState(ready)).toEqual({ state: "ready", missing: [] });
  });

  it("names the false checks in wiring order", () => {
    expect(factoryState({ webhook: false, labels: false, agentAccess: false, allowed: false })).toEqual({
      state: "missing",
      missing: ["webhook", "labels", "agent access", "not in ForgeHands' allowed list"],
    });
  });

  it("is missing when only the allowed list lacks the repository", () => {
    expect(factoryState({ webhook: true, labels: true, agentAccess: true, allowed: false })).toEqual({
      state: "missing",
      missing: ["not in ForgeHands' allowed list"],
    });
  });

  it("is unknown when a check is unknown and none is false", () => {
    expect(factoryState({ webhook: "unknown", labels: true, agentAccess: true, allowed: true })).toEqual({
      state: "unknown",
      missing: [],
    });
  });

  it("lets a false check win over an unknown one", () => {
    expect(factoryState({ webhook: "unknown", labels: false, agentAccess: "unknown", allowed: true })).toEqual({
      state: "missing",
      missing: ["labels"],
    });
  });
});

describe("RepositoriesView", () => {
  it("lists repositories sorted by name with visibility, branch and last update", () => {
    const html = render({
      repos: [
        repo({ name: "owner/zeta", private: false, default_branch: "trunk", updated: "2026-01-02T00:00:00Z", url: "http://forgejo.lab.example/owner/zeta" }),
        repo({ name: "owner/alpha" }),
      ],
    });
    expect(html.indexOf("http://forgejo.lab.example/owner/alpha")).toBeLessThan(html.indexOf("http://forgejo.lab.example/owner/zeta"));
    expect(text(html)).toContain("public");
    expect(text(html)).toContain("private");
    expect(html).toContain("trunk");
    expect(html).toContain(new Date("2026-01-02T00:00:00Z").toLocaleDateString());
    for (const head of ["Name", "Visibility", "Default branch", "Last update", "Factory"]) expect(html).toContain(head);
  });

  it("links the name to the repository only for an http(s) url", () => {
    const linked = render({ repos: [repo()] });
    expect(linked).toContain('href="http://forgejo.lab.example/owner/repository"');
    expect(linked).toContain('target="_blank"');
    expect(linked).toContain('rel="noopener noreferrer"');

    for (const url of ["", "javascript:alert(1)"]) {
      const plain = render({ repos: [repo({ url })] });
      expect(plain).not.toContain("<a");
      expect(plain).toContain("owner/repository");
    }
  });

  it("shows a dash when the last update is empty or unreadable", () => {
    expect(render({ repos: [repo({ updated: "" })] })).toContain("—");
    expect(render({ repos: [repo({ updated: "not a date" })] })).toContain("—");
  });

  it("shows a repository ready for the factory", () => {
    const html = render({ repos: [repo()] });
    expect(html).toContain("ready for factory");
    expect(html).toContain("border-ok");
    expect(html).toContain("text-ok");
  });

  it("names what is missing, in warning colour", () => {
    const html = render({ repos: [repo({ factory: { webhook: false, labels: false, agentAccess: true, allowed: true } })] });
    expect(text(html)).toContain("missing: webhook, labels");
    expect(html).toContain("border-warn");
    expect(html).toContain("text-warn");
  });

  it("says when the repository is not in ForgeHands' allowed list", () => {
    const html = render({ repos: [repo({ factory: { webhook: true, labels: true, agentAccess: true, allowed: false } })] });
    expect(text(html)).toContain("missing: not in ForgeHands' allowed list");
  });

  it("lists every missing check in wiring order", () => {
    const html = render({ repos: [repo({ factory: { webhook: false, labels: false, agentAccess: false, allowed: false } })] });
    expect(text(html)).toContain("missing: webhook, labels, agent access, not in ForgeHands' allowed list");
  });

  it("shows a neutral badge when Forgejo could not say", () => {
    const html = render({ repos: [repo({ factory: { webhook: "unknown", labels: true, agentAccess: true, allowed: true } })] });
    expect(html).toMatch(/>\s*unknown\s*</);
    expect(html).toContain('data-variant="outline"');
    expect(text(html)).toContain("could not be read");
    expect(html).not.toContain("ready for factory");
    expect(html).not.toContain("border-warn");
  });

  it("shows the missing check rather than unknown when both apply", () => {
    const html = render({ repos: [repo({ factory: { webhook: "unknown", labels: false, agentAccess: "unknown", allowed: true } })] });
    expect(text(html)).toContain("missing: labels");
    expect(html).not.toMatch(/>\s*unknown\s*</);
  });

  it("says when there are no repositories yet", () => {
    const html = render({ repos: [] });
    expect(html).toContain("No repositories yet.");
    expect(html).not.toContain("<table");
  });

  it("shows the error message instead of a table", () => {
    const html = render({ repos: [], error: "Forgejo is not reachable" });
    expect(html).toContain("Forgejo is not reachable");
    expect(html).toContain("warning");
    expect(html).not.toContain("<table");
    expect(html).not.toContain("No repositories yet.");
  });

  it("shows a skeleton while loading", () => {
    const html = render({ repos: [], loading: true });
    expect(html).toContain('data-slot="skeleton"');
    expect(html).not.toContain("No repositories yet.");
    expect(html).not.toContain("<table");
  });

  it("offers the New repository button only when there is somewhere to go", () => {
    expect(render({ repos: [repo()] })).not.toContain("New repository");
    expect(render({ repos: [repo()], onNew: () => undefined })).toContain("New repository");
    expect(render({ repos: [], onNew: () => undefined })).toContain("New repository");
  });
});
