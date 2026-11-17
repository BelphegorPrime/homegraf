import { mkdtempSync } from "node:fs";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { createApp } from "./app.js";
import { applier, type Spawn } from "./apply.js";
import type { Runner } from "./apps.js";
import { authenticator } from "./auth.js";
import { loadConfig } from "./config.js";
import { FACTORY_EVENTS, FACTORY_LABELS, ownerOf, repoSettings, type RepoSettings } from "./repos.js";
import { signedInReads, testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { toolPath } from "./tools.js";
import { userStore } from "./users.js";

const TOKEN = "tok-operator-never-shown-4711";
const SECRET = "hook-secret-never-shown-0815";
const WEBHOOK = "http://forgehands:8080/webhooks/forgejo";
const AGENT = "homegraf-agent";
const OWNER = "owner";

interface Answer {
  status: number;
  body?: unknown;
}

interface RepoState {
  name: string;
  private?: boolean;
  default_branch?: string;
  updated_at?: string;
  /** `null` leaves `html_url` out of the answer; anything else overrides the default. */
  html_url?: string | null;
  labels?: Answer;
  hooks?: Answer;
  permission?: Answer;
}

interface ForgejoState {
  owner: string;
  /** The owner is an organisation: /users answers 404, /orgs has the repositories. */
  org?: boolean;
  repos: RepoState[];
  /** The answer of the repository list itself, when reading it fails. */
  reposAnswer?: Answer;
  labels: Answer;
  hooks: Answer;
  permission: Answer;
}

const labelsAnswer = (names: readonly string[] = FACTORY_LABELS): Answer => ({
  status: 200,
  body: names.map((name, i) => ({ id: i + 1, name, color: "0e8a16", description: "" })),
});

const hook = (overrides: Record<string, unknown> = {}): Record<string, unknown> => ({
  id: 1,
  type: "forgejo",
  active: true,
  events: [...FACTORY_EVENTS],
  config: { url: WEBHOOK, content_type: "json", secret: SECRET },
  ...overrides,
});

const hooksAnswer = (hooks: unknown[] = [hook()]): Answer => ({ status: 200, body: hooks });

const permissionAnswer = (permission = "write"): Answer => ({ status: 200, body: { permission, role_name: permission } });

const closes: (() => Promise<void>)[] = [];
afterEach(async () => {
  for (const close of closes.splice(0)) await close();
});

/**
 * A Forgejo on loopback that answers the endpoints the routes read, with the fields and paths the real API has
 * (verified against Forgejo: /users/<owner>/repos, /repos/<owner>/<name>/labels, …/hooks, …/collaborators/<login>/permission).
 */
async function forgejo(initial: Partial<ForgejoState> = {}) {
  const state: ForgejoState = {
    owner: OWNER,
    repos: [{ name: "wired" }],
    labels: labelsAnswer(),
    hooks: hooksAnswer(),
    permission: permissionAnswer(),
    ...initial,
  };
  const requests: { method: string; path: string; authorization?: string }[] = [];
  const server = createServer((req, res) => {
    const url = new URL(req.url ?? "/", "http://127.0.0.1");
    requests.push({ method: req.method ?? "GET", path: url.pathname + url.search, authorization: req.headers.authorization });
    const send = (a: Answer) => {
      res.writeHead(a.status, { "content-type": "application/json" });
      res.end(JSON.stringify(a.body ?? { message: `HTTP ${a.status}`, url: "/api/swagger" }));
    };
    const page = Number(url.searchParams.get("page") ?? "1");
    const onPage = (a: Answer): Answer => (a.status === 200 && page > 1 ? { status: 200, body: [] } : a);
    const segments = url.pathname
      .split("/")
      .filter(Boolean)
      .map((s) => decodeURIComponent(s));
    if (segments[0] !== "api" || segments[1] !== "v1") return send({ status: 404 });
    const rest = segments.slice(2);
    const [kind, owner, name, part, one, two] = rest;

    if ((kind === "users" || kind === "orgs") && owner === state.owner && name === "repos" && rest.length === 3) {
      if (state.reposAnswer) return send(state.reposAnswer);
      if ((kind === "orgs") !== !!state.org) return send({ status: 404 });
      const items = state.repos.map((r, i) => ({
        id: i + 1,
        name: r.name,
        full_name: `${state.owner}/${r.name}`,
        private: r.private ?? true,
        default_branch: r.default_branch ?? "main",
        updated_at: r.updated_at ?? "2026-01-01T00:00:00Z",
        ...(r.html_url === null ? {} : { html_url: r.html_url ?? `http://forgejo.lab.example/${state.owner}/${r.name}` }),
      }));
      return send({ status: 200, body: items });
    }

    if (kind === "repos" && owner === state.owner) {
      const repo = state.repos.find((r) => r.name === name);
      if (!repo) return send({ status: 404 });
      if (part === "labels" && rest.length === 4) return send(onPage(repo.labels ?? state.labels));
      if (part === "hooks" && rest.length === 4) return send(onPage(repo.hooks ?? state.hooks));
      if (part === "collaborators" && one === AGENT && two === "permission" && rest.length === 6) return send(repo.permission ?? state.permission);
    }
    send({ status: 404 });
  });
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r));
  const baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  closes.push(() => new Promise<void>((r) => server.close(() => r())));
  const settings = (over: Partial<RepoSettings> = {}): RepoSettings => ({
    baseUrl,
    token: TOKEN,
    owner: OWNER,
    allowed: ["owner/wired"],
    agent: AGENT,
    webhookUrl: WEBHOOK,
    webhookSecret: SECRET,
    repoDir: "/repo",
    stateDir: "/tmp/homegraf-new-repo",
    ...over,
  });
  return { state, requests, baseUrl, settings };
}

/** A runner for `homelab.py repo-profiles`, recording its calls. */
function profileTool() {
  const calls: { file: string; args: string[]; root?: string }[] = [];
  const run: Runner = async (file, args, root) => {
    calls.push({ file, args, root });
    return {
      code: 0,
      out: JSON.stringify([
        { name: "python", description: "Python service with pytest", validate_command: "python -m pytest -q", files: ["AGENTS.md", ".gitignore"] },
        { name: "generic", description: "Anything else", validate_command: "make check", files: ["AGENTS.md"] },
      ]),
    };
  };
  return { calls, run };
}

/** A spawn for `python3 homelab.py new-repo`, recording what the job runs and printing a line. */
function repoCommand(log = "new repository owner/newrepo: created\n") {
  const calls: { cmd: string; args: string[]; cwd?: string; env: Record<string, string> }[] = [];
  const spawn: Spawn = async (cmd, args, o, onData) => {
    calls.push({ cmd, args, cwd: o.cwd, env: o.env });
    onData(log);
    return 0;
  };
  return { calls, spawn };
}

const env = { HOMEGRAF_PASSWORD: "pw-1234" };

async function build(options: { settings: RepoSettings; run?: Runner; spawn?: Spawn }) {
  const users = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
  await users.add("alice", "alice may create", ["apps:create"]);
  await users.add("bob", "bob may look", []);
  const app = createApp({
    loadGraph: async () => {
      throw new Error("unused");
    },
    status: { get: () => ({ updated: null, source: "disabled", nodes: {} }), subscribe: () => () => {} },
    webDir: "dist/web",
    plan: async () => {
      throw new Error("unused");
    },
    preview: async () => {
      throw new Error("unused");
    },
    addons: async () => [],
    templates: async () => [],
    unifi: () => ({ enabled: false }),
    certificates: { get: async () => ({ updated: null, certificates: [] }) },
    create: { problem: undefined, applier: applier({ cfg: loadConfig(env) }) },
    users: { enabled: true, store: users, authenticate: authenticator({ adminPassword: "pw-1234", users }) },
    removal: testRemoval(),
    creations: testCreations(),
    hosts: testHosts(),
    stacks: testStacks(),
    repos: options,
  });
  return signedInReads(app, "pw-1234");
}

const as = (name: string, password: string) => ({ authorization: `Basic ${Buffer.from(`${name}:${password}`).toString("base64")}` });

async function waitForJob(app: { request: (p: string, i?: RequestInit) => Response | Promise<Response> }, id: string) {
  for (let i = 0; i < 200; i++) {
    const job = (await (await app.request(`/api/repos/jobs/${id}`)).json()) as { state?: string };
    if (job.state !== "running") return job as { state: string; log: string; exitCode?: number };
    await new Promise((r) => setTimeout(r, 5));
  }
  throw new Error("the job did not finish");
}

describe("settings from the environment", () => {
  it("reads the Forgejo server, token, owner and allowed list; no token is exported anywhere else", () => {
    const s = repoSettings({
      FORGEJO_URL: "https://git.lab.example/",
      FORGEJO_TOKEN: TOKEN,
      HOMEGRAF_CLONE_URL: "https://git.lab.example/owner/infrastructure.git",
      FORGEHANDS_ALLOWED_REPOS: " owner/infrastructure , owner/other ",
      FORGEHANDS_AGENT_LOGIN: AGENT,
      DATA_DIR: "/data",
    });
    expect(s).toMatchObject({
      baseUrl: "https://git.lab.example",
      owner: "owner",
      allowed: ["owner/infrastructure", "owner/other"],
      agent: AGENT,
      stateDir: "/data/new-repo",
    });
    expect(ownerOf("https://git.example/owner/repository.git")).toBe("owner");
    expect(ownerOf(undefined)).toBeUndefined();
  });

  it("falls back to the clone URL's origin and the allowed list's owner", () => {
    expect(
      repoSettings({ HOMEGRAF_CLONE_URL: "https://git.example/owner/repo", FORGEHANDS_ALLOWED_REPOS: "owner/repo", FORGEHANDS_AGENT_LOGIN: "" }),
    ).toMatchObject({
      baseUrl: "https://git.example",
      owner: "owner",
    });
    expect(repoSettings({ FORGEHANDS_ALLOWED_REPOS: "team/repo", FORGEHANDS_AGENT_LOGIN: "" })).toMatchObject({
      owner: "team",
      agent: "openhands",
      allowed: ["team/repo"],
    });
  });
});

describe("listing repositories with the factory status", () => {
  it("shows the owner's repositories with a wired, a bare and an unreadable one", async () => {
    const f = await forgejo({
      repos: [
        { name: "wired" },
        { name: "bare", labels: { status: 200, body: [] }, hooks: { status: 200, body: [] }, permission: permissionAnswer("none") },
        { name: "hidden", labels: { status: 500 }, hooks: { status: 403 }, permission: { status: 403 } },
      ],
    });
    const app = await build({ settings: f.settings() });
    const res = await app.request("/api/repos");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({
      repos: [
        {
          name: "wired",
          private: true,
          default_branch: "main",
          updated: "2026-01-01T00:00:00Z",
          url: "http://forgejo.lab.example/owner/wired",
          factory: { webhook: true, labels: true, agentAccess: true, allowed: true },
        },
        {
          name: "bare",
          private: true,
          default_branch: "main",
          updated: "2026-01-01T00:00:00Z",
          url: "http://forgejo.lab.example/owner/bare",
          factory: { webhook: false, labels: false, agentAccess: false, allowed: false },
        },
        {
          name: "hidden",
          private: true,
          default_branch: "main",
          updated: "2026-01-01T00:00:00Z",
          url: "http://forgejo.lab.example/owner/hidden",
          factory: { webhook: "unknown", labels: "unknown", agentAccess: "unknown", allowed: false },
        },
      ],
    });
    // The token is the operator's: every request to Forgejo carries it, and no answer carries it back.
    expect(f.requests.length).toBeGreaterThan(0);
    expect(f.requests.every((r) => r.authorization === `token ${TOKEN}`)).toBe(true);
    for (const r of f.requests) expect(r.path).not.toContain(TOKEN);
  });

  it("gives a repository its html_url only when it is an http(s) address", async () => {
    const f = await forgejo({
      repos: [
        { name: "scripted", html_url: "javascript:alert(1)" },
        { name: "plain", html_url: null },
      ],
    });
    const app = await build({ settings: f.settings() });
    const body = (await (await app.request("/api/repos")).json()) as { repos: { name: string; url: string }[] };
    expect(body.repos).toMatchObject([
      { name: "scripted", url: "" },
      { name: "plain", url: "" },
    ]);
  });

  it("needs every factory label, one at a time", async () => {
    const f = await forgejo();
    const app = await build({ settings: f.settings() });
    for (const missing of FACTORY_LABELS) {
      f.state.labels = labelsAnswer(FACTORY_LABELS.filter((l) => l !== missing));
      const body = (await (await app.request("/api/repos")).json()) as { repos: { factory: { labels: unknown } }[] };
      expect(body.repos[0].factory.labels, `without ${missing}`).toBe(false);
    }
    f.state.labels = labelsAnswer();
    expect(((await (await app.request("/api/repos")).json()) as { repos: { factory: { labels: unknown } }[] }).repos[0].factory.labels).toBe(true);
  });

  it("needs every webhook event, the endpoint, an active hook and the JSON content type", async () => {
    const f = await forgejo();
    const app = await build({ settings: f.settings() });
    const webhook = async () => ((await (await app.request("/api/repos")).json()) as { repos: { factory: { webhook: unknown } }[] }).repos[0].factory.webhook;
    for (const missing of FACTORY_EVENTS) {
      f.state.hooks = hooksAnswer([hook({ events: FACTORY_EVENTS.filter((e) => e !== missing) })]);
      expect(await webhook(), `without ${missing}`).toBe(false);
    }
    f.state.hooks = hooksAnswer([hook({ active: false })]);
    expect(await webhook()).toBe(false);
    f.state.hooks = hooksAnswer([hook({ config: { url: "http://elsewhere:8080/webhooks/forgejo", content_type: "json" } })]);
    expect(await webhook()).toBe(false);
    f.state.hooks = hooksAnswer([hook({ config: { url: WEBHOOK, content_type: "form" } })]);
    expect(await webhook()).toBe(false);
    f.state.hooks = hooksAnswer([hook({ branch_filter: "main" })]);
    expect(await webhook()).toBe(false);
    f.state.hooks = hooksAnswer([hook({ config: { url: `${WEBHOOK}/`, content_type: "json" } })]);
    expect(await webhook()).toBe(true);
  });

  it("says unknown for the webhook when no endpoint is configured, and for the allowed list when it is not set", async () => {
    const f = await forgejo();
    const app = await build({ settings: f.settings({ webhookUrl: undefined, allowed: undefined }) });
    const body = (await (await app.request("/api/repos")).json()) as { repos: { factory: { webhook: unknown; allowed: unknown } }[] };
    expect(body.repos[0].factory).toMatchObject({ webhook: "unknown", allowed: "unknown" });
  });

  it("reads the repositories of an organisation under /orgs", async () => {
    const f = await forgejo({ owner: "team", org: true, repos: [{ name: "service" }] });
    const app = await build({ settings: f.settings({ owner: "team", allowed: ["team/service"] }) });
    const body = (await (await app.request("/api/repos")).json()) as { repos: { name: string; factory: { allowed: unknown } }[] };
    expect(body.repos).toMatchObject([{ name: "service", factory: { allowed: true } }]);
    expect(f.requests.some((r) => r.path.startsWith("/api/v1/orgs/team/repos"))).toBe(true);
  });

  it("fails the whole list when Forgejo cannot list the repositories, without the token in the answer", async () => {
    const f = await forgejo({ reposAnswer: { status: 500, body: { message: "boom" } } });
    const app = await build({ settings: f.settings() });
    const res = await app.request("/api/repos");
    expect(res.status).toBe(502);
    const text = await res.text();
    expect(text).toContain("HTTP 500");
    expect(text).not.toContain(TOKEN);
  });

  it("answers 503 with what is missing when it cannot read Forgejo at all", async () => {
    const app = await build({ settings: { agent: AGENT, repoDir: "/repo", stateDir: "/tmp/x" } });
    const res = await app.request("/api/repos");
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ error: expect.stringContaining("FORGEJO_TOKEN") });
  });
});

describe("repository profiles", () => {
  it("prints what homelab.py repo-profiles --json says", async () => {
    const f = await forgejo();
    const tool = profileTool();
    const app = await build({ settings: f.settings(), run: tool.run });
    const res = await app.request("/api/repo-profiles");
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual([
      { name: "python", description: "Python service with pytest", validate_command: "python -m pytest -q", files: ["AGENTS.md", ".gitignore"] },
      { name: "generic", description: "Anything else", validate_command: "make check", files: ["AGENTS.md"] },
    ]);
    expect(tool.calls).toEqual([{ file: toolPath("/repo", "homelab.py"), args: ["repo-profiles", "--json"], root: "/repo" }]);
  });
});

describe("creating a repository", () => {
  const plan = { name: "newrepo", profile: "python", description: "a small service", visibility: "public", branch: "trunk" };

  it("starts homelab.py new-repo as a job with the arguments of the form", async () => {
    const f = await forgejo();
    const tool = profileTool();
    const command = repoCommand();
    const app = await build({ settings: f.settings(), run: tool.run, spawn: command.spawn });
    const res = await app.request("/api/repos", {
      method: "POST",
      body: JSON.stringify(plan),
      headers: { "content-type": "application/json", ...as("alice", "alice may create") },
    });
    expect(res.status).toBe(202);
    const { jobId } = (await res.json()) as { jobId: string };
    expect(jobId).toEqual(expect.any(String));
    const job = await waitForJob(app, jobId);
    expect(job).toMatchObject({ state: "done", exitCode: 0 });
    expect(command.calls).toEqual([
      {
        cmd: "python3",
        args: [
          "-I",
          toolPath("/repo", "homelab.py"),
          "new-repo",
          "--name",
          "newrepo",
          "--profile",
          "python",
          "--description",
          "a small service",
          "--public",
          "--branch",
          "trunk",
          "--yes",
          "--json",
        ],
        cwd: "/repo",
        env: expect.objectContaining({
          HOMELAB_ROOT: "/repo",
          STATE_DIR: "/tmp/homegraf-new-repo",
          FORGEJO_URL: f.baseUrl,
          FORGEJO_TOKEN: TOKEN,
          FORGEHANDS_FORGEJO_WEBHOOK_URL: WEBHOOK,
          FORGEHANDS_FORGEJO_WEBHOOK_SECRET: SECRET,
          FORGEHANDS_AGENT_LOGIN: AGENT,
          GIT_TERMINAL_PROMPT: "0",
        }),
      },
    ]);
    // Nothing but what the tool needs is handed over.
    expect(command.calls[0].env).not.toHaveProperty("HOMEGRAF_PASSWORD");
    expect(JSON.stringify(command.calls[0].args)).not.toContain(TOKEN);
  });

  it("keeps the token and the webhook secret out of the job's log and the answers", async () => {
    const f = await forgejo();
    const command = repoCommand(`token ${TOKEN} and secret ${SECRET} leaked by a noisy tool\n`);
    const app = await build({ settings: f.settings(), run: profileTool().run, spawn: command.spawn });
    const res = await app.request("/api/repos", {
      method: "POST",
      body: JSON.stringify({ name: "newrepo", profile: "python" }),
      headers: { "content-type": "application/json", ...as("alice", "alice may create") },
    });
    const { jobId } = (await res.json()) as { jobId: string };
    const job = await waitForJob(app, jobId);
    expect(job.state).toBe("done");
    expect(job.log).toContain("*** and secret ***");
    const snapshot = await (await app.request(`/api/repos/jobs/${jobId}`)).text();
    const events = await (await app.request(`/api/repos/jobs/${jobId}/events`)).text();
    for (const text of [snapshot, events]) {
      expect(text).not.toContain(TOKEN);
      expect(text).not.toContain(SECRET);
      expect(text).toContain("***");
    }
    expect(await (await app.request("/api/repos")).text()).not.toContain(TOKEN);
    expect(await (await app.request("/api/repo-profiles")).text()).not.toContain(TOKEN);
  });

  it("refuses a user without apps:create", async () => {
    const f = await forgejo();
    const command = repoCommand();
    const app = await build({ settings: f.settings(), run: profileTool().run, spawn: command.spawn });
    const res = await app.request("/api/repos", {
      method: "POST",
      body: JSON.stringify(plan),
      headers: { "content-type": "application/json", ...as("bob", "bob may look") },
    });
    expect(res.status).toBe(403);
    expect(await res.json()).toMatchObject({ error: expect.stringContaining("apps:create") });
    expect(command.calls).toEqual([]);
  });

  it("answers 400 with the reason for a bad form", async () => {
    const f = await forgejo();
    const command = repoCommand();
    const app = await build({ settings: f.settings(), run: profileTool().run, spawn: command.spawn });
    const post = async (body: unknown) => {
      const res = await app.request("/api/repos", {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "content-type": "application/json", ...as("alice", "alice may create") },
      });
      return { status: res.status, body: (await res.json()) as Record<string, unknown> };
    };
    const unknown = await post({ name: "fine", profile: "ruby" });
    expect(unknown.status).toBe(400);
    expect(unknown.body).toMatchObject({ error: expect.stringContaining("unknown profile ruby") });
    for (const bad of [
      { name: "../escape", profile: "python" },
      { name: "fine", profile: "python", visibility: "secret" },
      { name: "fine", profile: "python", description: "   " },
      { name: "fine", profile: "python", branch: "main..next" },
      { name: "fine", profile: "python", branch: "main/" },
    ]) {
      const res = await post(bad);
      expect(res.status, JSON.stringify(bad)).toBe(400);
      expect(res.body).toMatchObject({ error: "invalid input" });
    }
    expect(command.calls).toEqual([]);
  });

  it("answers 503 and runs nothing when the ForgeHands wiring is not configured", async () => {
    const f = await forgejo();
    const command = repoCommand();
    const app = await build({ settings: f.settings({ webhookUrl: undefined }), run: profileTool().run, spawn: command.spawn });
    const res = await app.request("/api/repos", {
      method: "POST",
      body: JSON.stringify(plan),
      headers: { "content-type": "application/json", ...as("alice", "alice may create") },
    });
    expect(res.status).toBe(503);
    expect(await res.json()).toMatchObject({ error: expect.stringContaining("FORGEHANDS_FORGEJO_WEBHOOK_URL") });
    expect(command.calls).toEqual([]);
  });

  it("does not start a second job while one runs", async () => {
    const f = await forgejo();
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    const calls: string[][] = [];
    const spawn: Spawn = async (_cmd, args, _o, onData) => {
      calls.push(args);
      await held;
      onData("done\n");
      return 0;
    };
    const app = await build({ settings: f.settings(), run: profileTool().run, spawn });
    const post = async () => {
      const res = await app.request("/api/repos", {
        method: "POST",
        body: JSON.stringify(plan),
        headers: { "content-type": "application/json", ...as("alice", "alice may create") },
      });
      return res.status;
    };
    expect(await post()).toBe(202);
    expect(await post()).toBe(409);
    release();
    await new Promise((r) => setTimeout(r, 20));
    expect(calls).toHaveLength(1);
  });
});
