import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RepoProfileInfo } from "../shared/types";
import { NewRepoView, repoBody, validateRepo, type RepoFields } from "./NewRepo";

const profiles: RepoProfileInfo[] = [
  { name: "service", description: "A deployed service with a compose file", validate_command: "homelab.py check --profile service", files: ["compose.yaml"] },
  { name: "library", description: "A reusable library", validate_command: "python -m pytest", files: ["pyproject.toml"] },
];

const fields = (over: Partial<RepoFields> = {}): RepoFields => ({
  name: "example-repo",
  profile: "service",
  description: "A repository",
  visibility: "private",
  branch: "",
  ...over,
});

const render = (props: Partial<Parameters<typeof NewRepoView>[0]> = {}) =>
  renderToStaticMarkup(<NewRepoView profiles={profiles} fields={fields()} errors={{}} onChange={() => undefined} onSubmit={() => undefined} {...props} />);

describe("validateRepo", () => {
  it("accepts a filled form", () => {
    expect(validateRepo(fields())).toEqual({});
    expect(validateRepo(fields({ name: "a".repeat(100), branch: "feature/one" }))).toEqual({});
  });

  it("requires a name that follows the server's pattern", () => {
    expect(validateRepo(fields({ name: "" })).name).toMatch(/name/i);
    expect(validateRepo(fields({ name: "-bad" })).name).toBeDefined();
    expect(validateRepo(fields({ name: "a..b" })).name).toContain("..");
    expect(validateRepo(fields({ name: "a".repeat(101) })).name).toBeDefined();
  });

  it("requires a profile", () => {
    expect(validateRepo(fields({ profile: "  " })).profile).toBeDefined();
  });

  it("requires a description of at most 255 characters", () => {
    expect(validateRepo(fields({ description: "   " })).description).toBeDefined();
    expect(validateRepo(fields({ description: "a".repeat(256) })).description).toBeDefined();
    expect(validateRepo(fields({ description: "a".repeat(255) })).description).toBeUndefined();
  });

  it("accepts an empty branch but no malformed one", () => {
    expect(validateRepo(fields({ branch: "" })).branch).toBeUndefined();
    for (const branch of ["a//b", "x/", "x.", "a..b", "-main", "a".repeat(256)]) {
      expect(validateRepo(fields({ branch })).branch, branch).toBeDefined();
    }
  });
});

describe("repoBody", () => {
  it("trims the values and stays private by default", () => {
    expect(repoBody(fields({ name: "  example-repo ", profile: " service ", description: " a repository " }))).toEqual({
      name: "example-repo",
      profile: "service",
      description: "a repository",
      visibility: "private",
    });
  });

  it("leaves an empty branch out", () => {
    expect(repoBody(fields({ branch: "   " }))).not.toHaveProperty("branch");
  });

  it("passes a public repository and its branch through", () => {
    expect(repoBody(fields({ visibility: "public", branch: " release/1 " }))).toEqual({
      name: "example-repo",
      profile: "service",
      description: "A repository",
      visibility: "public",
      branch: "release/1",
    });
  });
});

describe("NewRepoView", () => {
  it("lists every profile and the details of the selected one", () => {
    const html = render({ fields: fields({ profile: "library" }) });
    expect(html).toContain("service");
    expect(html).toContain("library");
    expect(html).toContain("A reusable library");
    expect(html).toContain("python -m pytest");
    expect(html).not.toContain("A deployed service with a compose file");
  });

  it("shows the profile-list error", () => {
    const html = render({ error: "homelab.py repo-profiles failed: no settings" });
    expect(html).toContain("homelab.py repo-profiles failed: no settings");
    expect(html).toContain("warning");
  });

  it("shows that the profiles are loading", () => {
    expect(render({ loading: true })).toContain("Loading profiles");
  });

  it("shows the messages of the fields", () => {
    const html = render({ errors: { name: "Enter a name.", branch: "a branch name without .., //, or a trailing / or ." } });
    expect(html).toContain("Enter a name.");
    expect(html).toContain("a branch name without .., //, or a trailing / or .");
    expect(html).toContain('class="error"');
  });

  it("offers private before public, with the branch placeholder main", () => {
    const html = render();
    expect(html.indexOf("private")).toBeLessThan(html.indexOf("public"));
    expect(html).toContain('placeholder="main"');
  });
});
