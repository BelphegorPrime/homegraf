import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { RepoFactoryStatus, RepoInfo, RepoJobSnapshot, RepoProfileInfo } from "../shared/types";
import { NewRepoView, RepoProgress, repoBody, repoForJob, validateRepo, type RepoFields } from "./NewRepo";

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

const factory = (over: Partial<RepoFactoryStatus> = {}): RepoFactoryStatus => ({ webhook: true, labels: true, agentAccess: true, allowed: true, ...over });

const repo = (over: Partial<RepoInfo> = {}): RepoInfo => ({
  name: "example-repo",
  private: true,
  default_branch: "main",
  updated: "2026-03-04T05:06:07Z",
  url: "http://forgejo.lab.example/owner/example-repo",
  factory: factory(),
  ...over,
});

const snapshot = (over: Partial<RepoJobSnapshot> = {}): RepoJobSnapshot => ({
  id: "job-1",
  repository: "example-repo",
  state: "done",
  log: "repository created\n",
  ...over,
});

const progress = (job: Parameters<typeof RepoProgress>[0]["job"], r?: RepoInfo) => renderToStaticMarkup(<RepoProgress job={job} repo={r} />);

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

  it("disables the submit button while the job runs", () => {
    expect(render({ busy: true })).toMatch(/<button[^>]* disabled=""[^>]*>Create repository/);
    expect(render()).not.toMatch(/<button[^>]* disabled=""[^>]*>Create repository/);
  });

  it("shows a 409 from the server as the form error", () => {
    const html = render({ error: "another repository is being created, wait for it to finish" });
    expect(html).toContain("warning");
    expect(html).toContain("another repository is being created, wait for it to finish");
  });
});

describe("repoForJob", () => {
  it("finds the repository by its name or its owner-prefixed name", () => {
    expect(repoForJob([repo()], "example-repo")?.name).toBe("example-repo");
    expect(repoForJob([repo({ name: "owner/example-repo" })], "example-repo")?.name).toBe("owner/example-repo");
    expect(repoForJob([repo({ name: "owner/other" })], "example-repo")).toBeUndefined();
  });
});

describe("RepoProgress", () => {
  it("shows the log and a creating badge while the job runs", () => {
    const html = progress({ log: "writing files\n" });
    expect(html).toContain("creating");
    expect(html).toContain("writing files");
    expect(html).toContain("<pre>");
    expect(html).not.toContain("created");
    expect(html).not.toContain("stopped");
  });

  it("shows a stopped badge, the log and the exit code when the job failed", () => {
    const html = progress({ log: "boom\n", done: snapshot({ state: "failed", exitCode: 2, log: "boom\n" }) });
    expect(html).toContain("stopped");
    expect(html).toContain("boom");
    expect(html).toContain("exit code 2");
    expect(html).toContain("warning");
    expect(html).toContain("sent again");
  });

  it("links the created repository and reminds that the operator writes the first tickets", () => {
    const html = progress({ log: "done\n", done: snapshot() }, repo());
    expect(html).toContain("created");
    expect(html).toContain('href="http://forgejo.lab.example/owner/example-repo"');
    expect(html).toContain("first tickets");
    expect(html).toContain("written by the operator");
    expect(html).toContain("issues that exist");
  });

  it("shows no link when the repository has no http(s) url", () => {
    const html = progress({ log: "done\n", done: snapshot() }, repo({ url: "" }));
    expect(html).toContain("created");
    expect(html).toContain("The repository is created.");
    expect(html).not.toContain("href=");
  });

  it("shows the --wire next step only when the allowed list lacks the repository", () => {
    const missing = progress({ log: "done\n", done: snapshot() }, repo({ factory: factory({ allowed: false }) }));
    expect(missing).toContain("--wire");
    expect(missing).toContain("new-repo ... --wire");
    expect(missing).toContain("ALLOWED_REPOS");
    expect(missing).toContain("MERGE_POLICY");
    expect(missing).toContain("pull request");
    expect(missing).toContain("nothing is deployed");

    expect(progress({ log: "done\n", done: snapshot() }, repo())).not.toContain("--wire");
    const unknown = progress({ log: "done\n", done: snapshot() }, repo({ factory: factory({ allowed: "unknown" }) }));
    expect(unknown).not.toContain("--wire");
    expect(unknown).toContain("could not be read");
  });

  it("escapes the log", () => {
    const html = progress({ log: "<script>alert('x')</script>\n" });
    expect(html).not.toContain("<script>");
    expect(html).toContain("&lt;script&gt;");
  });
});
