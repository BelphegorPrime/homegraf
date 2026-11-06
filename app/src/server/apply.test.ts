import { describe, expect, it } from "vitest";
import { signedInReads, testCreations, testHosts, testRemoval, testStacks } from "./testing.js";
import { createApp } from "./app.js";
import { applier, type Spawn } from "./apply.js";
import { authenticator } from "./auth.js";
import { userStore } from "./users.js";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createProblem, loadConfig } from "./config.js";
import { planInput } from "./apps.js";

const env = {
  KOMODO_URL: "http://komodo:9120",
  KOMODO_API_KEY: "kkey",
  KOMODO_API_SECRET: "ksecret",
  HOMEGRAF_PASSWORD: "pw-1234",
  HOMEGRAF_CLONE_URL: "https://git.example/owner/infrastructure.git",
  FORGEJO_TOKEN: "ftoken",
  INFISICAL_URL: "https://inf.example",
  INFISICAL_BOOTSTRAP_CLIENT_ID: "iid",
  INFISICAL_BOOTSTRAP_CLIENT_SECRET: "isecret",
};
const cfg = loadConfig(env);
const input = planInput.parse({ appId: "demo-app", server: "aws", image: "x:1", containerPort: 80 });

interface Call {
  cmd: string;
  args: string[];
  env: Record<string, string>;
  cwd?: string;
}

function fakeSpawn(tool: (onData: (s: string) => void) => Promise<number> | number = () => 0) {
  const calls: Call[] = [];
  const spawn: Spawn = async (cmd, args, o, onData) => {
    calls.push({ cmd, args, env: o.env, cwd: o.cwd });
    return cmd === "python3" ? tool(onData) : 0;
  };
  return { calls, spawn };
}
const settled = (job: { subscribers: Set<unknown> }) =>
  new Promise<void>((r) => {
    const f = () => r();
    job.subscribers.add((c: string | null) => c === null && f());
  });
const opts = (spawn: Spawn, exists = false) => ({ cfg, spawn, exists: () => exists, login: async () => "owner" });

describe("createProblem", () => {
  it("is off until everything is configured", () => {
    expect(createProblem(cfg.create, cfg.komodo)).toBeUndefined();
    const partial = loadConfig({ ...env, FORGEJO_TOKEN: "", INFISICAL_URL: "" });
    expect(createProblem(partial.create, partial.komodo)).toMatch(/FORGEJO_TOKEN.*INFISICAL_URL/);
  });
});

describe("applier", () => {
  it("clones when needed, resets to origin/main, then runs new-app --pr --yes and finds the pull request", async () => {
    const { calls, spawn } = fakeSpawn((out) => {
      out("  ✓ ok\n  pull request: https://git.example/owner/infrastructure/pulls/7\n");
      return 0;
    });
    const a = applier(opts(spawn));
    const job = a.start(input)!;
    await settled(job);
    expect(job).toMatchObject({ state: "done", exitCode: 0, pullRequestUrl: "https://git.example/owner/infrastructure/pulls/7" });
    expect(calls.map((c) => [c.cmd, ...c.args.filter((x) => x !== "-C" && x !== "/work")].slice(0, 3))).toEqual([
      ["git", "clone", "-q"],
      ["git", "switch", "-q"],
      ["git", "reset", "-q"],
      ["git", "clean", "-fdq"],
      ["python3", "-I", "/work/homelab.py"],
    ]);
    const tool = calls.at(-1)!;
    expect(tool.args).toEqual(expect.arrayContaining(["new-app", "--app-id", "demo-app", "--pr", "--yes"]));
    expect(tool.cwd).toBe("/work");
  });

  it("fetches instead of cloning when the clone exists", async () => {
    const { calls, spawn } = fakeSpawn();
    const job = applier(opts(spawn, true)).start(input)!;
    await settled(job);
    expect(calls[0].args).toContain("fetch");
    expect(calls.some((c) => c.args.includes("clone"))).toBe(false);
  });

  it("gives the tool its credentials, git only the header, and never puts a token on a command line", async () => {
    const { calls, spawn } = fakeSpawn();
    const job = applier(opts(spawn)).start(input)!;
    await settled(job);
    const tool = calls.at(-1)!;
    expect(tool.env).toMatchObject({ KOMODO_API_SECRET: "ksecret", FORGEJO_TOKEN: "ftoken", INFISICAL_BOOTSTRAP_CLIENT_SECRET: "isecret" });
    expect(tool.env).not.toHaveProperty("HOMEGRAF_PASSWORD");
    const git = calls[0];
    expect(git.env.GIT_CONFIG_VALUE_0).toBe(`Authorization: Basic ${Buffer.from("owner:ftoken").toString("base64")}`);
    expect(git.env).not.toHaveProperty("KOMODO_API_SECRET");
    for (const c of calls) expect(c.args.join(" ")).not.toMatch(/ftoken|isecret|ksecret|pw-1234/);
  });

  it("scrubs secret values and colour codes from the output", async () => {
    const { spawn } = fakeSpawn((out) => {
      out("\x1b[32mtoken ftoken and pw-1234\x1b[0m\n");
      return 0;
    });
    const job = applier(opts(spawn)).start(input)!;
    await settled(job);
    expect(job.log).toContain("token *** and ***");
    expect(job.log).not.toContain("ftoken");
  });

  it("hands over an edited compose file as a temporary file and typed secrets through the environment, scrubbed from the log", async () => {
    let compose = "";
    const { readFileSync } = await import("node:fs");
    const calls: Call[] = [];
    const spawn: Spawn = async (cmd, args, o, onData) => {
      calls.push({ cmd, args, env: o.env, cwd: o.cwd });
      if (cmd === "python3") {
        compose = readFileSync(args[args.indexOf("--compose-file") + 1], "utf8");
        onData("value typed-secret-42 stored\n");
      }
      return 0;
    };
    const edited = planInput.parse({
      appId: "demo-app",
      server: "aws",
      compose: "services:\n  web:\n    image: x:1\n",
      secretValues: { DB_PASS: "typed-secret-42" },
    });
    const job = applier(opts(spawn)).start(edited)!;
    await settled(job);
    const tool = calls.at(-1)!;
    expect(compose).toBe("services:\n  web:\n    image: x:1\n");
    expect(tool.args).toEqual(expect.arrayContaining(["--generate-secrets", "--secret", "DB_PASS=env:SECRET_VALUE_DB_PASS", "--pr", "--yes"]));
    expect(tool.args.join(" ")).not.toContain("typed-secret-42");
    expect(tool.env.SECRET_VALUE_DB_PASS).toBe("typed-secret-42");
    expect(job.log).toContain("value *** stored");
  });

  it("reports a failing tool and a failing git step", async () => {
    const failing = applier(opts(fakeSpawn(() => 1).spawn)).start(input)!;
    await settled(failing);
    expect(failing).toMatchObject({ state: "failed", exitCode: 1 });

    const spawn: Spawn = async (cmd) => (cmd === "git" ? 128 : 0);
    const job = applier(opts(spawn)).start(input)!;
    await settled(job);
    expect(job.state).toBe("failed");
    expect(job.log).toMatch(/git clone failed \(exit 128\)/);
  });

  it("refuses a second job while one runs, and accepts one afterwards", async () => {
    let release!: () => void;
    const { spawn } = fakeSpawn(() => new Promise<number>((r) => (release = () => r(0))));
    const a = applier(opts(spawn));
    const first = a.start(input)!;
    expect(a.start(input)).toBeUndefined();
    await new Promise((r) => setTimeout(r, 10));
    release();
    await settled(first);
    expect(a.start(input)).toBeDefined();
  });
});

const basic = (name: string, password: string) => `Basic ${Buffer.from(`${name}:${password}`).toString("base64")}`;

describe("create routes", () => {
  async function build(problem?: string, maxFailures = 5) {
    const { calls, spawn } = fakeSpawn((out) => {
      out("pull request: https://git.example/pulls/1\n");
      return 0;
    });
    const store = userStore(join(mkdtempSync(join(tmpdir(), "homegraf-")), "users.json"));
    await store.add("alice", "alice has apps rights", ["apps:create"]);
    await store.add("bob", "bob can only look", []);
    const app = signedInReads(
      createApp({
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
        removal: testRemoval(),
        creations: testCreations(),
        hosts: testHosts(),
        stacks: testStacks(),
        create: { problem, applier: applier(opts(spawn)) },
        users: { enabled: true, store, authenticate: authenticator({ adminPassword: "pw-1234", users: store, maxFailures }) },
      }),
      "pw-1234",
    );
    const post = (body: unknown, auth?: string) =>
      app.request("/api/apps/create", {
        method: "POST",
        body: JSON.stringify(body),
        headers: { "content-type": "application/json", ...(auth ? { authorization: auth } : {}) },
      });
    return { app, post, calls };
  }
  const demo = { appId: "demo-app", server: "aws" };

  it("reports whether creating and signing in are available", async () => {
    expect(await (await (await build()).app.request("/api/capabilities")).json()).toEqual({
      create: { enabled: true },
      users: { enabled: true },
      unifi: { enabled: false },
    });
    expect(await (await (await build("not configured: X")).app.request("/api/capabilities")).json()).toEqual({
      create: { enabled: false, reason: "not configured: X" },
      users: { enabled: true },
      unifi: { enabled: false },
    });
  });

  it("answers 503 when not configured, 401 without or with wrong credentials, 429 when locked", async () => {
    expect((await (await build("not configured: X")).post(demo, basic("admin", "pw-1234"))).status).toBe(503);
    const { post } = await build(undefined, 2);
    expect((await post(demo)).status).toBe(401);
    expect((await post(demo, basic("admin", "no"))).status).toBe(401);
    expect((await post(demo, basic("admin", "no"))).status).toBe(401);
    expect((await post(demo, basic("admin", "pw-1234"))).status).toBe(429);
  });

  it("answers 403 for a user without apps:create and starts nothing", async () => {
    const { post, calls } = await build();
    const res = await post({ ...demo, image: "x:1", containerPort: 80 }, basic("bob", "bob can only look"));
    expect(res.status).toBe(403);
    expect((await res.json()).error).toMatch(/bob may not do this \(needs apps:create\)/);
    expect(calls).toEqual([]);
  });

  it("checks who it is before the input, then validates the input", async () => {
    const { post } = await build();
    expect((await post({ appId: "Bad_Id", server: "aws" }, basic("admin", "no"))).status).toBe(401);
    const bad = await post({ appId: "Bad_Id", server: "aws" }, basic("alice", "alice has apps rights"));
    expect(bad.status).toBe(400);
    expect((await bad.json()).issues[0].field).toBe("appId");
  });

  it("starts a job for the user, who is the author of the pull request, and streams its output", async () => {
    const { app, post, calls } = await build();
    const res = await post({ ...demo, image: "x:1", containerPort: 80 }, basic("alice", "alice has apps rights"));
    expect(res.status).toBe(202);
    const { jobId } = await res.json();
    const text = await (await app.request(`/api/apps/jobs/${jobId}/events`)).text();
    expect(text).toContain("event: log");
    expect(text).toContain("event: done");
    expect(text).toContain("https://git.example/pulls/1");
    expect(calls.find((c) => c.cmd === "python3")!.env.GIT_AUTHOR_NAME).toBe("alice via homegraf");
    const snap = await (await app.request(`/api/apps/jobs/${jobId}`)).json();
    expect(snap).toMatchObject({ state: "done", appId: "demo-app", requestedBy: "alice", pullRequestUrl: "https://git.example/pulls/1" });
    expect(JSON.stringify(snap)).not.toContain("pw-1234");
    expect(JSON.stringify(snap)).not.toContain("alice has apps rights");
  });

  it("answers 404 for an unknown job", async () => {
    const { app } = await build();
    expect((await app.request("/api/apps/jobs/nope")).status).toBe(404);
    expect((await app.request("/api/apps/jobs/nope/events")).status).toBe(404);
  });
});
